// Shared Windows/Linux/Android transport. Steam social APIs live in their helper;
// this process uses the standalone GNS library and our authenticated TLS signaling.
#include "crossplay_net.h"
#ifndef TTT2_HAVE_GNS
#define TTT2_HAVE_GNS 0
#endif
#if TTT2_HAVE_GNS
#include <steam/steamnetworkingsockets.h>
#include <steam/isteamnetworkingutils.h>
#include <steam/steamnetworkingcustomsignaling.h>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <map>
#include <mutex>
#include <thread>
#include <vector>

namespace crossplay {
namespace {
struct Packet { uint64_t peer; bool reliable; std::vector<uint8_t> bytes; };
std::mutex queueMutex;
std::deque<Packet> queue;
size_t queuedBytes = 0;
std::atomic<bool> running{false};
std::atomic<int> relayedPeers{0};
std::thread worker;
std::shared_ptr<community::Client> server;
Receiver receiver = nullptr;
ISteamNetworkingSockets* sockets = nullptr;
HSteamListenSocket listener = k_HSteamListenSocket_Invalid;
HSteamNetPollGroup pollGroup = k_HSteamNetPollGroup_Invalid;
std::map<uint64_t, HSteamNetConnection> connections; // service thread only
struct Shutdown { ~Shutdown() { Stop(); } } shutdown;
std::string IdentityText(uint64_t peer) {
    char text[32]; std::snprintf(text, sizeof(text), "ttt2-%016llx", (unsigned long long)peer); return text;
}
SteamNetworkingIdentity Identity(uint64_t peer) {
    SteamNetworkingIdentity id{}; id.SetGenericString(IdentityText(peer).c_str()); return id;
}
uint64_t Peer(const SteamNetworkingIdentity& id) {
    const char* text = id.GetGenericString();
    if (!text || std::string(text).size() != 21 || std::string(text, 5) != "ttt2-") return 0;
    char* end = nullptr; const auto peer = std::strtoull(text + 5, &end, 16);
    return end && !*end && peer >> 48 == 0x4350 ? peer : 0;
}
struct Signaling : ISteamNetworkingConnectionSignaling {
    std::shared_ptr<community::Client> client;
    uint64_t to;
    Signaling(std::shared_ptr<community::Client> c, uint64_t p) : client(std::move(c)), to(p) {}
    bool SendSignal(HSteamNetConnection, const SteamNetConnectionInfo_t&, const void* data, int size) override {
        return size > 0 && client->SendSignal(to, data, size_t(size));
    }
    void Release() override { delete this; }
};
struct Incoming : ISteamNetworkingSignalingRecvContext {
    uint64_t from;
    explicit Incoming(uint64_t p) : from(p) {}
    ISteamNetworkingConnectionSignaling* OnConnectRequest(HSteamNetConnection conn, const SteamNetworkingIdentity& id, int port) override {
        if (port != 0 || Peer(id) != from || !server->ConnectionPeers().count(from)) return nullptr;
        connections[from] = conn;
        sockets->SetConnectionPollGroup(conn, pollGroup);
        sockets->AcceptConnection(conn);
        return new Signaling(server, from);
    }
    void SendRejectionSignal(const SteamNetworkingIdentity&, const void*, int) override {}
};

// Pin the protobuf's sender identity to its authenticated envelope even for an
// EXISTING connection (GNS only calls OnConnectRequest for new ones). Schema is
// pinned with the GNS dependency: CMsgSteamNetworkingP2PRendezvous field 8.
bool SignalFrom(const std::vector<uint8_t>& b, uint64_t from) {
    size_t at = 0; bool found = false;
    auto varint = [&](uint64_t& value) {
        value = 0;
        for (unsigned shift = 0; shift < 64 && at < b.size(); shift += 7) {
            const uint8_t v = b[at++];
            if (shift == 63 && v > 1) return false;
            value |= uint64_t(v & 127) << shift;
            if (!(v & 128)) return true;
        }
        return false;
    };
    while (at < b.size()) {
        uint64_t tag, n = 0;
        if (!varint(tag) || !(tag >> 3)) return false;
        if ((tag & 7) == 0) { if (!varint(n)) return false; continue; }
        if ((tag & 7) == 1) n = 8;
        else if ((tag & 7) == 5) n = 4;
        else if ((tag & 7) == 2) { if (!varint(n)) return false; }
        else return false;
        if (n > b.size() - at) return false;
        if ((tag >> 3) == 8) {
            if (found || (tag & 7) != 2 || std::string(reinterpret_cast<const char*>(b.data() + at), size_t(n)) != "str:" + IdentityText(from)) return false;
            found = true;
        }
        at += size_t(n);
    }
    return found;
}

// xsocket's 12-byte little-endian header, following our channel byte. A search
// lease permits only bounded UDP 10000 probes/replies, never gameplay or files.
bool DiscoveryPacket(const uint8_t* data, size_t size) {
    return size >= 13 && size <= 4097 && data[0] == 0 && data[1] == 1 && data[2] == 17 &&
        data[5] == 0x10 && data[6] == 0x27 && data[7] == 0x10 && data[8] == 0x27;
}

void Run() {
    while (running && server->Connected()) {
        std::string relayUser, relayPassword;
        if (server->TakeRelayCredentials(relayUser, relayPassword)) {
            SteamNetworkingUtils()->SetGlobalConfigValueString(k_ESteamNetworkingConfig_P2P_TURN_UserList, relayUser.c_str());
            SteamNetworkingUtils()->SetGlobalConfigValueString(k_ESteamNetworkingConfig_P2P_TURN_PassList, relayPassword.c_str());
        }
        const auto peers = server->ConnectionPeers();
        const auto members = server->RoomPeers();
        int relayed = 0;
        for (auto it = connections.begin(); it != connections.end();) {
            SteamNetConnectionInfo_t info{};
            if (!peers.count(it->first) || !sockets->GetConnectionInfo(it->second, &info) ||
                info.m_eState == k_ESteamNetworkingConnectionState_ClosedByPeer ||
                info.m_eState == k_ESteamNetworkingConnectionState_ProblemDetectedLocally) {
                std::fprintf(stderr, "[crossplay] peer %016llx closed: %s\n", (unsigned long long)it->first, info.m_szEndDebug);
                sockets->CloseConnection(it->second, 0, "Room ended or peer disconnected", false);
                it = connections.erase(it);
            } else {
                if (info.m_eState == k_ESteamNetworkingConnectionState_Connected && (info.m_nFlags & k_nSteamNetworkConnectionInfoFlags_Relayed)) ++relayed;
                ++it;
            }
        }
        relayedPeers = relayed;
        uint64_t from; std::vector<uint8_t> signal;
        while (server->TakeSignal(from, signal)) {
            if (peers.count(from) && SignalFrom(signal, from)) {
                Incoming ctx(from); sockets->ReceivedP2PCustomSignal(signal.data(), int(signal.size()), &ctx);
            }
        }
        {
            std::lock_guard l(queueMutex);
            while (!queue.empty()) {
                const auto& p = queue.front();
                if (peers.count(p.peer) && (members.count(p.peer) || DiscoveryPacket(p.bytes.data(), p.bytes.size()))) {
                    auto& conn = connections[p.peer];
                    if (!conn) {
                        auto id = Identity(p.peer);
                        SteamNetworkingConfigValue_t option;
                        option.SetInt32(k_ESteamNetworkingConfig_SymmetricConnect, 1);
                        conn = sockets->ConnectP2PCustomSignaling(new Signaling(server, p.peer), &id, 0, 1, &option);
                        if (conn) sockets->SetConnectionPollGroup(conn, pollGroup);
                    }
                    if (conn) {
                        const int flags = p.reliable ? k_nSteamNetworkingSend_Reliable : k_nSteamNetworkingSend_UnreliableNoNagle;
                        const auto result = sockets->SendMessageToConnection(conn, p.bytes.data(), uint32_t(p.bytes.size()), flags, nullptr);
                        // Preserve reliable order under backpressure; Send refuses new packets once our bounded queue fills.
                        if (p.reliable && result == k_EResultLimitExceeded) break;
                        if (result != k_EResultOK && p.reliable) {
                            sockets->CloseConnection(conn, 0, "Reliable send failed", false);
                            connections.erase(p.peer);
                        }
                    } else connections.erase(p.peer);
                }
                queuedBytes -= p.bytes.size(); queue.pop_front();
            }
        }
        SteamNetworkingMessage_t* messages[64];
        const int n = sockets->ReceiveMessagesOnPollGroup(pollGroup, messages, 64);
        for (int i = 0; i < n; ++i) {
            auto* m = messages[i]; const uint64_t peer = Peer(m->m_identityPeer);
            if (peers.count(peer) && m->m_cbSize >= 1 && m->m_cbSize <= 65537) {
                const auto* data = static_cast<const uint8_t*>(m->m_pData);
                if (data[0] <= 1 && receiver && (members.count(peer) || DiscoveryPacket(data, size_t(m->m_cbSize)))) receiver(peer, data[0] != 0, data + 1, size_t(m->m_cbSize - 1));
            }
            m->Release();
        }
        sockets->RunCallbacks();
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    running = false;
    // Revoke gameplay immediately when the authenticated control connection is lost.
    for (const auto& [peer, conn] : connections) sockets->CloseConnection(conn, 0, "Server disconnected", false);
    connections.clear(); relayedPeers = 0;
}
} // namespace
bool Available() { return true; }
int RelayedPeers() { return relayedPeers.load(); }
void Stop() {
    running = false;
    if (worker.joinable()) worker.join();
    if (sockets) {
        for (const auto& [peer, conn] : connections) sockets->CloseConnection(conn, 0, "Left network", false);
        connections.clear();
        if (listener) sockets->CloseListenSocket(listener);
        if (pollGroup) sockets->DestroyPollGroup(pollGroup);
        GameNetworkingSockets_Kill();
    }
    sockets = nullptr; listener = 0; pollGroup = 0; server.reset(); receiver = nullptr;
    std::lock_guard l(queueMutex); queue.clear(); queuedBytes = 0;
}
bool Start(std::shared_ptr<community::Client> c, const community::Welcome& w, Receiver recv, std::string& error) {
    Stop();
    auto identity = Identity(w.peer); SteamNetworkingErrMsg err{};
    if (!GameNetworkingSockets_Init(&identity, err)) { error = err; return false; }
    sockets = SteamNetworkingSockets(); server = std::move(c); receiver = recv;
    auto* utils = SteamNetworkingUtils();
    utils->SetDebugOutputFunction(std::getenv("TTT2_CROSSPLAY_NETLOG") ? k_ESteamNetworkingSocketsDebugOutputType_Verbose : k_ESteamNetworkingSocketsDebugOutputType_Warning,
        [](ESteamNetworkingSocketsDebugOutputType, const char* message) { std::fprintf(stderr, "[gns] %s\n", message); });
    utils->SetGlobalConfigValueInt32(k_ESteamNetworkingConfig_P2P_Transport_ICE_Implementation, 1);
    utils->SetGlobalConfigValueInt32(k_ESteamNetworkingConfig_P2P_Transport_ICE_Enable,
        std::getenv("TTT2_CROSSPLAY_FORCE_RELAY") ? 1 : 7);
    utils->SetGlobalConfigValueString(k_ESteamNetworkingConfig_P2P_STUN_ServerList, w.stunServer.c_str());
    utils->SetGlobalConfigValueString(k_ESteamNetworkingConfig_P2P_TURN_ServerList, w.turnServer.c_str());
    utils->SetGlobalConfigValueString(k_ESteamNetworkingConfig_P2P_TURN_UserList, w.turnUser.c_str());
    utils->SetGlobalConfigValueString(k_ESteamNetworkingConfig_P2P_TURN_PassList, w.turnPassword.c_str());
    SteamNetworkingConfigValue_t option; option.SetInt32(k_ESteamNetworkingConfig_SymmetricConnect, 1);
    listener = sockets->CreateListenSocketP2P(0, 1, &option);
    pollGroup = sockets->CreatePollGroup();
    if (!listener || !pollGroup) { error = "Could not initialize cross-platform connections."; Stop(); return false; }
    running = true; worker = std::thread(Run); return true;
}
bool Send(uint64_t peer, bool reliable, const void* data, size_t size) {
    if (!running || !data || size > 65536) return false;
    std::lock_guard l(queueMutex);
    if (queue.size() >= 512 || queuedBytes + size + 1 > 1024 * 1024) return false;
    Packet p{peer, reliable, {uint8_t(reliable)}};
    const auto* bytes = static_cast<const uint8_t*>(data);
    p.bytes.insert(p.bytes.end(), bytes, bytes + size);
    queuedBytes += p.bytes.size(); queue.push_back(std::move(p)); return true;
}
} // namespace crossplay
#else
namespace crossplay {
bool Available() { return false; }
int RelayedPeers() { return 0; }
bool Start(std::shared_ptr<community::Client>, const community::Welcome&, Receiver, std::string& error) {
    error = "This build does not include cross-platform networking."; return false;
}
void Stop() {}
bool Send(uint64_t, bool, const void*, size_t) { return false; }
}
#endif
