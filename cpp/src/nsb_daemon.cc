// nsb_daemon.cc

#include "nsb_daemon.h"
#include <cstring>

namespace nsb {

    namespace {
        constexpr int kServerPollTimeoutMs = 10000;
        constexpr int kWritablePollTimeoutMs = 10000;

        std::uint64_t steadyNowNs() {
            return static_cast<std::uint64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(
                    std::chrono::steady_clock::now().time_since_epoch()
                ).count()
            );
        }

        MessageEntry::TraceEntry traceFromMetadata(const nsb::nsbm::Metadata& metadata) {
            MessageEntry::TraceEntry trace;
            if (metadata.has_trace()) {
                const auto& proto_trace = metadata.trace();
                trace.msg_id = proto_trace.msg_id();
                trace.t_app_send_ns = proto_trace.t_app_send_ns();
                trace.t_daemon_send_ingress_ns = proto_trace.t_daemon_send_ingress_ns();
                trace.t_daemon_fetch_egress_ns = proto_trace.t_daemon_fetch_egress_ns();
                trace.t_daemon_post_ingress_ns = proto_trace.t_daemon_post_ingress_ns();
                trace.t_daemon_receive_egress_ns = proto_trace.t_daemon_receive_egress_ns();
            }
            return trace;
        }

        void applyTraceToMetadata(const MessageEntry::TraceEntry& trace, nsb::nsbm::Metadata* metadata) {
            if (!trace.hasData()) {
                return;
            }
            nsb::nsbm::Metadata::Trace* proto_trace = metadata->mutable_trace();
            proto_trace->set_msg_id(trace.msg_id);
            proto_trace->set_t_app_send_ns(trace.t_app_send_ns);
            proto_trace->set_t_daemon_send_ingress_ns(trace.t_daemon_send_ingress_ns);
            proto_trace->set_t_daemon_fetch_egress_ns(trace.t_daemon_fetch_egress_ns);
            proto_trace->set_t_daemon_post_ingress_ns(trace.t_daemon_post_ingress_ns);
            proto_trace->set_t_daemon_receive_egress_ns(trace.t_daemon_receive_egress_ns);
        }

        bool waitForWritable(int fd, const char* context) {
            pollfd writable_fd{fd, POLLOUT, 0};
            while (true) {
                int ready = poll(&writable_fd, 1, kWritablePollTimeoutMs);
                if (ready > 0) {
                    if ((writable_fd.revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) {
                        LOG(ERROR) << context
                                   << ": socket became unavailable while waiting to write."
                                   << std::endl;
                        return false;
                    }
                    return (writable_fd.revents & POLLOUT) != 0;
                }
                if (ready == 0) {
                    LOG(ERROR) << context << ": timed out waiting for socket to become writable."
                               << std::endl;
                    return false;
                }
                if (errno == EINTR) {
                    continue;
                }
                LOG(ERROR) << context << ": poll() failed while waiting for writable state: "
                           << strerror(errno) << std::endl;
                return false;
            }
        }

        bool sendAll(int fd, const void* buffer, std::size_t size, const char* context) {
            const char* bytes = static_cast<const char*>(buffer);
            std::size_t total_sent = 0;
            while (total_sent < size) {
                ssize_t bytes_sent = send(fd, bytes + total_sent, size - total_sent, 0);
                if (bytes_sent > 0) {
                    total_sent += static_cast<std::size_t>(bytes_sent);
                    continue;
                }
                if (bytes_sent == 0) {
                    LOG(ERROR) << context << ": send() returned 0 bytes." << std::endl;
                    return false;
                }
                if (errno == EINTR) {
                    continue;
                }
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    if (!waitForWritable(fd, context)) {
                        return false;
                    }
                    continue;
                }
                LOG(ERROR) << context << ": send() failed: " << strerror(errno) << std::endl;
                return false;
            }
            return true;
        }
    }

    NSBDaemon::NSBDaemon(int s_port, std::string filename) : running(false), server_port(s_port) {
        GOOGLE_PROTOBUF_VERIFY_VERSION;
        configure(filename);
    }

    NSBDaemon::~NSBDaemon() {
        // If the server is running, stop it.
        if (running) {
            stop();
        }
        google::protobuf::ShutdownProtobufLibrary();
    }

    void NSBDaemon::start() {
        // If the server isn't running already, start it.
        if (!running) {
            running = true;
            start_server(server_port);
            LOG(INFO) << "NSBDaemon started." << std::endl;
        }
    }

    void NSBDaemon::configure(std::string filename) {
        // Open YAML file.
        YAML::Node config = YAML::LoadFile(filename);
        // Check if the file is valid.
        if (config.IsNull()) {
            std::cerr << "Failed to load configuration file: " << filename << std::endl;
            return;
        }
        // Parse the configuration file.
        int sys_mode = config["system"]["mode"].as<int>();
        cfg.SYSTEM_MODE = static_cast<Config::SystemMode>(sys_mode);
        int sim_mode = config["system"]["simulator_mode"].as<int>();
        cfg.SIMULATOR_MODE = static_cast<Config::SimulatorMode>(sim_mode);
        cfg.USE_DB = config["database"]["use_db"].as<bool>();
        if (cfg.USE_DB) {
            cfg.DB_ADDRESS = config["database"]["db_address"].as<std::string>();
            cfg.DB_PORT = config["database"]["db_port"].as<int>();
        }
    }

    void NSBDaemon::start_server(int port) {
        // Set file descriptor and address information.
        int server_fd = socket(AF_INET, SOCK_STREAM, 0);
        if (server_fd == -1) {
            perror("Socket creation failed.");
            return;
        }
        // Set to accept multiple connections.
        const int opt = 1;
        if (setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt)) < 0) {
            perror("Set socket options failed.");
            close(server_fd);
            return;
        }
        // Set non-blocking.
        int flags = fcntl(server_fd, F_GETFL, 0);
        if (flags == -1) {
            perror("Get socket flags failed.");
            close(server_fd);
            return;
        }
        if (fcntl(server_fd, F_SETFL, flags | O_NONBLOCK) == -1) {
            perror("Set socket flags failed.");
            close(server_fd);
            return;
        }
        // Create address.
        sockaddr_in server_addr{};
        server_addr.sin_family = AF_INET;
        server_addr.sin_addr.s_addr = inet_addr("0.0.0.0");
        server_addr.sin_port = htons(port);
        // Bind and listen on port.
        if (bind(server_fd, (struct sockaddr*)&server_addr, sizeof(server_addr)) == -1) {
            std::string error_msg = "Bind failed on address " + std::string(inet_ntoa(server_addr.sin_addr)) + 
                                    " on port " + std::to_string(ntohs(server_addr.sin_port)) + ".";
            LOG(ERROR) << error_msg << std::endl;
            close(server_fd);
            return;
        }
        if (listen(server_fd, SOMAXCONN) == -1) {
            LOG(ERROR) << "Listen failed." << std::endl;
            close(server_fd);
            return;
        }
        LOG(INFO) << "Server started on port " << port << std::endl;

        // Run server.
        std::vector<pollfd> poll_fds;
        poll_fds.push_back({server_fd, POLLIN, 0});
        while (running) {
            int activity = poll(poll_fds.data(), poll_fds.size(), kServerPollTimeoutMs);
            if (activity < 0) {
                if (errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK) {
                    LOG(ERROR) << "poll() failed in daemon main loop: " << strerror(errno)
                               << std::endl;
                    break;
                }
                continue;
            }
            if (activity == 0) {
                continue;
            }

            if ((poll_fds.front().revents & POLLIN) != 0) {
                while (true) {
                    sockaddr_in client_addr{};
                    socklen_t client_len = sizeof(client_addr);
                    int channel_fd = accept(server_fd, (struct sockaddr*)&client_addr, &client_len);
                    if (channel_fd == -1) {
                        if (errno == EAGAIN || errno == EWOULDBLOCK) {
                            break;
                        }
                        LOG(ERROR) << "Accept failed: " << strerror(errno) << std::endl;
                        break;
                    }
                    int channel_flags = fcntl(channel_fd, F_GETFL, 0);
                    if (channel_flags == -1 ||
                        fcntl(channel_fd, F_SETFL, channel_flags | O_NONBLOCK) == -1) {
                        LOG(ERROR) << "Failed to set accepted channel to non-blocking." << std::endl;
                        close(channel_fd);
                        continue;
                    }
                    char client_ip[INET_ADDRSTRLEN];
                    inet_ntop(AF_INET, &(client_addr.sin_addr), client_ip, INET_ADDRSTRLEN);
                    int client_port = ntohs(client_addr.sin_port);
                    LOG(INFO) << "Channel connected from IP: " << client_ip 
                            << ", Port: " << client_port << "." << std::endl;
                    // Add to the FD lookup.
                    std::string key = std::string(client_ip) + ":" + std::to_string(client_port);
                    fd_lookup.insert_or_assign(key, channel_fd);
                    poll_fds.push_back({channel_fd, POLLIN, 0});
                }
            }

            for (std::size_t index = 1; index < poll_fds.size();) {
                pollfd& channel = poll_fds[index];
                int fd = channel.fd;
                short revents = channel.revents;
                if (revents == 0) {
                    ++index;
                    continue;
                }

                bool disconnect = false;
                if ((revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) {
                    disconnect = true;
                } else if ((revents & POLLIN) != 0) {
                    char buffer[MAX_BUFFER_SIZE];
                    while (true) {
                        ssize_t bytes_read = recv(fd, buffer, sizeof(buffer), 0);
                        if (bytes_read > 0) {
                            DLOG(INFO) << "Picked up " << bytes_read << "B from FD " << fd << "." << std::endl;
                            connection_buffers[fd].insert(connection_buffers[fd].end(), buffer, buffer + bytes_read);
                            continue;
                        }
                        if (bytes_read == 0) {
                            disconnect = true;
                            break;
                        }
                        if (errno == EINTR) {
                            continue;
                        }
                        if (errno == EAGAIN || errno == EWOULDBLOCK) {
                            break;
                        }
                        LOG(ERROR) << "recv() failed on FD " << fd << ": " << strerror(errno)
                                   << std::endl;
                        disconnect = true;
                        break;
                    }

                    while (connection_buffers[fd].size() >= 4) {
                        uint32_t msg_size_net;
                        std::memcpy(&msg_size_net, connection_buffers[fd].data(), 4);
                        uint32_t msg_size = ntohl(msg_size_net);

                        if (connection_buffers[fd].size() >= 4 + msg_size) {
                            std::vector<char> message(connection_buffers[fd].begin() + 4, connection_buffers[fd].begin() + 4 + msg_size);
                            connection_buffers[fd].erase(connection_buffers[fd].begin(), connection_buffers[fd].begin() + 4 + msg_size);
                            
                            DLOG(INFO) << "Received message from FD " << fd << ": "
                                       << std::string(message.begin(), message.end()) << std::endl;
                            handle_message(fd, message);
                        } else {
                            break;
                        }
                    }
                }

                if (disconnect) {
                    LOG(WARNING) << "Disconnected from FD " << fd << "." << std::endl;
                    shutdown(fd, SHUT_WR);
                    close(fd);
                    connection_buffers.erase(fd);
                    poll_fds.erase(poll_fds.begin() + static_cast<std::ptrdiff_t>(index));
                    continue;
                }

                ++index;
            }
        }
        LOG(INFO) << "Server is no longer running, closing connections..." << std::endl;
        // When running stops, close connections and close server.
        for (std::size_t index = 1; index < poll_fds.size(); ++index) {
            DLOG(INFO) << "Closing connection to FD " << poll_fds[index].fd << "." << std::endl;
            close(poll_fds[index].fd);
        }
        close(server_fd);
        LOG(INFO) << "Server stopped." << std::endl;
    }

    void NSBDaemon::handle_message(int fd, std::vector<char> message) {
        nsb::nsbm nsb_message;
        if (!nsb_message.ParseFromArray(message.data(), static_cast<int>(message.size()))) {
            LOG(ERROR) << "Failed to parse incoming protobuf message from FD " << fd << "." << std::endl;
            return;
        }
        nsb::nsbm::Manifest manifest = nsb_message.manifest();
        DLOG(INFO) << "Manifest " << nsb::nsbm::Manifest::Operation_Name(manifest.op()) << "<--" 
                   << nsb::nsbm::Manifest::Originator_Name(manifest.og())
                   << " received from FD " << fd << "." << std::endl;
        // Prepare template for response.
        nsb::nsbm nsb_response;
        nsb::nsbm::Manifest* r_manifest = nsb_response.mutable_manifest();
        bool response_required = false;
        // Redirect handling based on specified operation.
        switch (manifest.op()) {
            case nsb::nsbm::Manifest::INIT:
                handle_init(&nsb_message, &nsb_response, &response_required);
                break;
            case nsb::nsbm::Manifest::PING:
                handle_ping(&nsb_message, &nsb_response, &response_required);
                break;
            case nsb::nsbm::Manifest::SEND:
                handle_send(&nsb_message, &nsb_response, &response_required);
                break;
            case nsb::nsbm::Manifest::FETCH:
                handle_fetch(&nsb_message, &nsb_response, &response_required);
                break;
            case nsb::nsbm::Manifest::POST:
                handle_post(&nsb_message, &nsb_response, &response_required);
                break;
            case nsb::nsbm::Manifest::RECEIVE:
                handle_receive(&nsb_message, &nsb_response, &response_required);
                break;
            case nsb::nsbm::Manifest::EXIT:
                LOG(INFO) << "Exiting." << std::endl;
                // Stop the daemon.
                stop();
                break;
            default:
                LOG(ERROR) << "Unknown operation: " << manifest.op() << std::endl;
                // Create a negative PING response if confused.
                r_manifest->set_op(nsb::nsbm::Manifest::PING);
                r_manifest->set_og(nsb::nsbm::Manifest::DAEMON);
                r_manifest->set_code(nsb::nsbm::Manifest::FAILURE);
                // response_required = true;
        }
        // Send response if required.
        if (response_required) {
            std::size_t size = nsb_response.ByteSizeLong();
            void* r_buffer = malloc(size);
            if (r_buffer == nullptr) {
                LOG(ERROR) << "Failed to allocate response buffer." << std::endl;
                return;
            }
            if (!nsb_response.SerializeToArray(r_buffer, static_cast<int>(size))) {
                LOG(ERROR) << "Failed to serialize daemon response." << std::endl;
                free(r_buffer);
                return;
            }
            DLOG(INFO) << "Sending response back: (" << size << "B) " << r_buffer << std::endl;
            if (!sendAll(fd, r_buffer, size, "Failed to send daemon response")) {
                free(r_buffer);
                return;
            }
            free(r_buffer);
        }
    }

    void NSBDaemon::handle_init(nsb::nsbm* incoming_msg, nsb::nsbm* outgoing_msg, bool* response_required) {
        bool success = false;
        *response_required = false;
        LOG(INFO) << "Handling INIT message from client " 
                << incoming_msg->intro().identifier() << "..." << std::endl;
        // Get client details.
        if (incoming_msg->has_intro()) {
            if (incoming_msg->manifest().og() == nsb::nsbm::Manifest::APP_CLIENT) {
                app_client_lookup.emplace(incoming_msg->intro().identifier(), ClientDetails(incoming_msg, fd_lookup));
                success = true;
            } else if (incoming_msg->manifest().og() == nsb::nsbm::Manifest::SIM_CLIENT) {
                if (cfg.SIMULATOR_MODE == Config::SimulatorMode::PER_NODE) {
                    // If per-node simulator mode, use the identifier as the key.
                    sim_client_lookup.emplace(incoming_msg->intro().identifier(), ClientDetails(incoming_msg, fd_lookup));
                    success = true;
                } else if (cfg.SIMULATOR_MODE == Config::SimulatorMode::SYSTEM_WIDE) {
                    // If system-wide simulator mode, check that there isn't already one.
                    if (sim_client_lookup.size() > 0) {
                        LOG(ERROR) << "\tSystem-wide simulator mode only allows for one simulator client." << std::endl;
                    } else {
                        // Use a generic key as it's not important.
                        sim_client_lookup.emplace("simulator", ClientDetails(incoming_msg, fd_lookup));
                        success = true;
                    }
                }
            } else {
                LOG(ERROR) << "\tUnknown/unexpected originator." << std::endl;
                return;
            }
        } else {
            LOG(ERROR) << "\tNo client details provided in INIT message." << std::endl;
            return;
        }
        *response_required = true;
        // Send back configuration details.
        nsb::nsbm::Manifest* out_manifest = outgoing_msg->mutable_manifest();
        out_manifest->set_op(nsb::nsbm::Manifest::INIT);
        out_manifest->set_og(nsb::nsbm::Manifest::DAEMON);
        out_manifest->set_code(success ? nsb::nsbm::Manifest::SUCCESS : nsb::nsbm::Manifest::FAILURE);
        nsb::nsbm::ConfigParams* out_config = outgoing_msg->mutable_config();
        out_config->set_sys_mode(static_cast<nsb::nsbm::ConfigParams::SystemMode>(cfg.SYSTEM_MODE));
        out_config->set_use_db(cfg.USE_DB);
        out_config->set_sim_mode(static_cast<nsb::nsbm::ConfigParams::SimulatorMode>(cfg.SIMULATOR_MODE));
        LOG(INFO) << "\tReturning configuration: Mode " << nsb::nsbm::ConfigParams::SystemMode(out_config->sys_mode())
                << " | Use DB? " << out_config->use_db() << std::endl;
        if (cfg.USE_DB) {
            out_config->set_db_address(cfg.DB_ADDRESS);
            out_config->set_db_port(cfg.DB_PORT);
            out_config->set_db_num(cfg.DB_NUM);
        }
        LOG(INFO) << "\tDatabase Address: " << cfg.DB_ADDRESS << " | Database Port: " << cfg.DB_PORT << std::endl;
    }

    void NSBDaemon::handle_ping(nsb::nsbm* incoming_msg, nsb::nsbm* outgoing_msg, bool* response_required) {
        LOG(INFO) << "Received PING from " << incoming_msg->metadata().src_id() << "." << std::endl;
        nsb::nsbm::Manifest* out_manifest = outgoing_msg->mutable_manifest();
        out_manifest->set_op(nsb::nsbm::Manifest::PING);
        out_manifest->set_og(nsb::nsbm::Manifest::DAEMON);
        out_manifest->set_code(nsb::nsbm::Manifest::SUCCESS);
        *response_required = true;
    }

    void NSBDaemon::handle_send(nsb::nsbm* incoming_msg, nsb::nsbm* outgoing_msg, bool* response_required) {
        *response_required = false;
        LOG(INFO) << "Handling SEND message from client " 
                << incoming_msg->intro().identifier() << " in " << std::endl;
        nsb::nsbm::Metadata in_metadata = incoming_msg->metadata();
        MessageEntry::TraceEntry trace = traceFromMetadata(in_metadata);
        trace.t_daemon_send_ingress_ns = steadyNowNs();
        if (cfg.SYSTEM_MODE == Config::SystemMode::PULL) {
            LOG(INFO).NoPrefix() << "PULL mode..." << std::endl;
            // Retrieve payload if using database, otherwise no need.
            std::string payload_obj = msg_get_payload_obj(incoming_msg);
            // Store payload.
            MessageEntry msg_entry = MessageEntry(
                in_metadata.src_id(),
                in_metadata.dest_id(),
                payload_obj,
                in_metadata.payload_size(),
                trace
            );
            DLOG(INFO) << "TX entry created | " 
                << in_metadata.payload_size() << " B | src: " 
                << msg_entry.source << " | dest: " 
                << msg_entry.destination << std::endl;
            DLOG(INFO) << (cfg.USE_DB ? "\tPayload ID: ": "\tPayload: ") << msg_entry.payload_obj << std::endl;
            // Add it to the buffer.
            tx_buffer.push_back(msg_entry);
        } else if (cfg.SYSTEM_MODE == Config::SystemMode::PUSH) {
            LOG(INFO).NoPrefix() << "PUSH mode..." << std::endl;
            // Copy the incoming message to the outgoing message, replacing with SEND to FORWARD.
            outgoing_msg->Clear();
            outgoing_msg->MergeFrom(*incoming_msg);
            nsb::nsbm::Manifest* out_manifest = outgoing_msg->mutable_manifest();
            out_manifest->set_op(nsb::nsbm::Manifest::FORWARD);
            applyTraceToMetadata(trace, outgoing_msg->mutable_metadata());
            // Select the target simulator if multiple simulator clients are used, else select the first and only one.
            ClientDetails target_sim;
            if (cfg.SIMULATOR_MODE == Config::SimulatorMode::SYSTEM_WIDE) {
                // If system-wide simulator client, just get the only client in the lookup.
                target_sim = sim_client_lookup.begin()->second;
            } else if (cfg.SIMULATOR_MODE == Config::SimulatorMode::PER_NODE) {
                // If per-node simulator client, use the source ID to specify the target sim.
                target_sim = sim_client_lookup.at(incoming_msg->metadata().src_id());
            } else {
                LOG(ERROR) << "No simulator clients available to forward message." << std::endl;
                return;
            }
            DLOG(INFO) << "Attempting to forward message to sim RECV channel (FD:" 
                << target_sim.ch_RECV_fd << ")..." << std::endl;
            outgoing_msg->mutable_metadata()->mutable_trace()->set_t_daemon_fetch_egress_ns(steadyNowNs());
            // Serialize the message and send it to the sim RECV channel.
            std::size_t size = outgoing_msg->ByteSizeLong();
            void* buffer = malloc(size);
            if (buffer == nullptr) {
                LOG(ERROR) << "Failed to allocate forwarding buffer." << std::endl;
                return;
            }
            if (!outgoing_msg->SerializeToArray(buffer, static_cast<int>(size))) {
                LOG(ERROR) << "Failed to serialize message for simulator forwarding." << std::endl;
                free(buffer);
                return;
            }
            if (!sendAll(target_sim.ch_RECV_fd, buffer, size, "Failed to forward message to simulator")) {
                free(buffer);
                return;
            }
            DLOG(INFO) << "\tForwarded message to sim RECV channel (" << size << " B)" << std::endl;
            free(buffer);
        }
    }

    void NSBDaemon::handle_fetch(nsb::nsbm* incoming_msg, nsb::nsbm* outgoing_msg, bool* response_required) {
        *response_required = false;
        DLOG(INFO) << "Handling FETCH message on behalf of " << incoming_msg->metadata().src_id() << std::endl;
        MessageEntry fetched_message;
        // Check to see if source has been specified.
        if (incoming_msg->has_metadata()) {
            nsb::nsbm::Metadata in_metadata = incoming_msg->metadata();
            if (!in_metadata.src_id().empty()) {
                // Search for the message in the buffer.
                auto it = std::find_if(tx_buffer.begin(), tx_buffer.end(),
                          [&](const auto& msg) { return msg.source == in_metadata.src_id(); });
                if (it != tx_buffer.end()) {
                    fetched_message = *it;
                    tx_buffer.erase(it);
                }
            } else {
                // If source not specified, pop the next message in the queue.
                if (!tx_buffer.empty()) {
                    fetched_message = tx_buffer.front();
                    tx_buffer.pop_front();
                }
            }
        }
        if (fetched_message.exists()) {
            DLOG(INFO) << "TX entry retrieved | " 
                       << fetched_message.payload_size << " B | src: " 
                       << fetched_message.source << " | dest: " 
                       << fetched_message.destination << std::endl;
            DLOG(INFO) << "\tPayload: " << fetched_message.payload_obj << std::endl;
        }
        
        // Prepare response.
        nsb::nsbm::Manifest* out_manifest = outgoing_msg->mutable_manifest();
        out_manifest->set_op(nsb::nsbm::Manifest::FETCH);
        out_manifest->set_og(nsb::nsbm::Manifest::DAEMON);
        // If message was found (MessageEntry populated), reply with message.
        if (fetched_message.exists()) {
            out_manifest->set_code(nsb::nsbm::Manifest::MESSAGE);
            nsb::nsbm::Metadata* out_metadata = outgoing_msg->mutable_metadata();
            out_metadata->set_src_id(fetched_message.source);
            out_metadata->set_dest_id(fetched_message.destination);
            out_metadata->set_payload_size(static_cast<int>(fetched_message.payload_size));
            fetched_message.trace.t_daemon_fetch_egress_ns = steadyNowNs();
            applyTraceToMetadata(fetched_message.trace, out_metadata);
            msg_set_payload_obj(fetched_message.payload_obj, outgoing_msg);
        } else {
            // Otherwise, indicate no message was fetched.
            out_manifest->set_code(nsb::nsbm::Manifest::NO_MESSAGE);
        }
        *response_required = true;
    }

    void NSBDaemon::handle_post(nsb::nsbm* incoming_msg, nsb::nsbm* outgoing_msg, bool* response_required) {
        *response_required = false;
        LOG(INFO) << "Handling POST message from client " 
                << incoming_msg->intro().identifier() << " in " << std::endl;
        nsb::nsbm::Metadata in_metadata = incoming_msg->metadata();
        MessageEntry::TraceEntry trace = traceFromMetadata(in_metadata);
        trace.t_daemon_post_ingress_ns = steadyNowNs();
        if (cfg.SYSTEM_MODE == Config::SystemMode::PULL) {
            LOG(INFO).NoPrefix() << "PULL mode..." << std::endl;
            // Check for message.
            nsb::nsbm::Manifest in_manifest = incoming_msg->manifest();
            if (in_manifest.code() == nsb::nsbm::Manifest::MESSAGE) {
                // Retrieve payload if using database, otherwise no need.
                std::string payload_obj = msg_get_payload_obj(incoming_msg);
                // Store payload.
                MessageEntry msg_entry = MessageEntry(
                    in_metadata.src_id(),
                    in_metadata.dest_id(),
                    payload_obj,
                    in_metadata.payload_size(),
                    trace
                );
                DLOG(INFO) << "RX entry created | " 
                        << in_metadata.payload_size() << " B | src: " 
                        << msg_entry.source << " | dest: " 
                        << msg_entry.destination << "\n\tPayload: " 
                        << msg_entry.payload_obj << std::endl;
                rx_buffer.push_back(msg_entry);
            }
        } else if (cfg.SYSTEM_MODE == Config::SystemMode::PUSH) {
            LOG(INFO).NoPrefix() << "PUSH mode..." << std::endl;
            // Copy the incoming message to the outgoing message, replacing with SEND to FORWARD.
            outgoing_msg->Clear();
            outgoing_msg->MergeFrom(*incoming_msg);
            nsb::nsbm::Manifest* out_manifest = outgoing_msg->mutable_manifest();
            out_manifest->set_op(nsb::nsbm::Manifest::FORWARD);
            applyTraceToMetadata(trace, outgoing_msg->mutable_metadata());
            // Get the destination to forward to.
            std::string dest_id = incoming_msg->metadata().dest_id();
            int target_fd = (app_client_lookup.find(dest_id) != app_client_lookup.end()) ? app_client_lookup[dest_id].ch_RECV_fd : -1;
            // Send to sim via RECV channel.
            if (target_fd != -1) {
                DLOG(INFO) << "Attempting to forward message to " 
                        << dest_id << " RECV channel (FD:" 
                        << target_fd << ")..." << std::endl;
                outgoing_msg->mutable_metadata()->mutable_trace()->set_t_daemon_receive_egress_ns(steadyNowNs());
                // Serialize the message and send it to the target RECV channel.
                std::size_t size = outgoing_msg->ByteSizeLong();
                void* buffer = malloc(size);
                if (buffer == nullptr) {
                    LOG(ERROR) << "Failed to allocate forwarding buffer." << std::endl;
                    return;
                }
                if (!outgoing_msg->SerializeToArray(buffer, static_cast<int>(size))) {
                    LOG(ERROR) << "Failed to serialize message for app forwarding." << std::endl;
                    free(buffer);
                    return;
                }
                if (!sendAll(target_fd, buffer, size, "Failed to forward message to app client")) {
                    free(buffer);
                    return;
                }
                DLOG(INFO) << "\tForwarded message to " 
                        << dest_id << " RECV channel (" 
                        << size << " B)" << std::endl;
                free(buffer);
            } else {
                DLOG(ERROR) << "No destination FD found for forwarding to " 
                            << dest_id << "." << std::endl;
            }
        }
    }

    void NSBDaemon::handle_receive(nsb::nsbm* incoming_msg, nsb::nsbm* outgoing_msg, bool* response_required) {
        LOG(INFO) << "Handling RECEIVE message from client " 
                << incoming_msg->intro().identifier() << "." << std::endl;
        MessageEntry received_message;
        // Check for destination.
        if (incoming_msg->has_metadata()) {
            nsb::nsbm::Metadata in_metadata = incoming_msg->metadata();
            if (!in_metadata.dest_id().empty()) {
                // Search for the message in the buffer.
                auto it = std::find_if(rx_buffer.begin(), rx_buffer.end(),
                          [&](const auto& msg) { return msg.destination == in_metadata.dest_id(); });
                if (it != rx_buffer.end()) {
                    received_message = *it;
                    rx_buffer.erase(it);
                }
            } else {
                // If destination not specified, pop the next message in the queue.
                if (!rx_buffer.empty()) {
                    received_message = rx_buffer.front();
                    rx_buffer.pop_front();
                }
            }
        }
        if (received_message.exists()) {
            DLOG(INFO) << "RX entry retrieved | " 
                << received_message.payload_size << " B | src: " 
                << received_message.source << " | dest: " 
                << received_message.destination << "\n\tPayload: " 
                << received_message.payload_obj << std::endl;
        } else {
            DLOG(INFO) << "No (matching) entries found." << std::endl;
        }
        // Prepare response.
        nsb::nsbm::Manifest* out_manifest = outgoing_msg->mutable_manifest();
        out_manifest->set_op(nsb::nsbm::Manifest::RECEIVE);
        out_manifest->set_og(nsb::nsbm::Manifest::DAEMON);
        // If message was found (MessageEntry populated), reply with message.
        if (received_message.exists()) {
            out_manifest->set_code(nsb::nsbm::Manifest::MESSAGE);
            nsb::nsbm::Metadata* out_metadata = outgoing_msg->mutable_metadata();
            out_metadata->set_src_id(received_message.source);
            out_metadata->set_dest_id(received_message.destination);
            out_metadata->set_payload_size(static_cast<int>(received_message.payload_size));
            received_message.trace.t_daemon_receive_egress_ns = steadyNowNs();
            applyTraceToMetadata(received_message.trace, out_metadata);
            msg_set_payload_obj(received_message.payload_obj, outgoing_msg);
        } else {
            // Otherwise, indicate no message found.
            out_manifest->set_code(nsb::nsbm::Manifest::NO_MESSAGE);
        }
        *response_required = true;
    }

    void NSBDaemon::stop() {
        // If the server is running, stop it.
        if (running) {
            running = false;
            LOG(INFO) << "NSBDaemon stopped." << std::endl;
        }
    }

    bool NSBDaemon::is_running() const {
        return running;
    }
}

/**
 * @brief Main process to run the NSB Daemon.
 * 
 * @return int 
 */
int main(int argc, char *argv[]) {
    using namespace nsb;
    // Set up logging.
    NsbLogSink log_output = NsbLogSink();
    absl::InitializeLog();
    absl::log_internal::AddLogSink(&log_output);
    // Check argument.
    if (argc != 2) {
        LOG(ERROR) << "Usage: " << argv[0] << " <config_file>" << std::endl;
        return 1;
    }
    // Check if the provided config file exists.
    if (access(argv[1], F_OK) == -1) {
        LOG(ERROR) << "Configuration file does not exist: " << argv[1] << std::endl;
        return 1;
    }
    // Start daemon.
    LOG(INFO) << "Starting daemon...\n";
    NSBDaemon daemon = NSBDaemon(65432, argv[1]);
    daemon.start();
    daemon.stop();
    LOG(INFO) << "Exit.";
    return 0;
}
