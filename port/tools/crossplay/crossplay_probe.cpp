// Two independent processes exercise real TLS signaling and encrypted GNS traffic.
#include "community_net.h"
#include "crossplay_net.h"
#include "server_list.h"
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <thread>
#include <vector>
#include <cstdlib>
namespace {
std::atomic<int> received{0}, unreliableReceived{0};
std::atomic<bool> bad{false};
std::atomic<int> discovered{0};
const uint8_t discoveryPacket[16] = {1,17,0,0,0x10,0x27,0x10,0x27,0,0,0,0,'p','i','n','g'};
void Probe(const std::shared_ptr<community::Client>& client) {
    for (const auto& [peer, steam] : client->ConnectionPeers())
        crossplay::Send(peer, false, discoveryPacket, sizeof(discoveryPacket));
}
void Receive(uint64_t, bool reliable, const uint8_t* data, size_t size) {
    if (!reliable && size == sizeof(discoveryPacket) && !std::memcmp(data, discoveryPacket, size)) { ++discovered; return; }
    if (size != 1024) { bad = true; return; }
    const unsigned sequence = unsigned(data[0]) | unsigned(data[1]) << 8;
    if (sequence >= 100) bad = true;
    for (size_t i=2;i<size;++i) if (data[i] != uint8_t(sequence+i)) bad = true;
    if (reliable) { if (sequence != unsigned(received.fetch_add(1))) bad = true; }
    else ++unreliableReceived;
}
void Put32(std::vector<uint8_t>& v, uint32_t n) { for (int i=0;i<4;++i) v.push_back(uint8_t(n >> (8*i))); }
void Put64(std::vector<uint8_t>& v, uint64_t n) { Put32(v,uint32_t(n)); Put32(v,uint32_t(n>>32)); }
void BE(std::vector<uint8_t>& v, size_t at, uint64_t n) { for(int i=0;i<8;++i) v[at+i]=uint8_t(n >> (56-8*i)); }
}
int main(int argc, char** argv) {
    if (argc != 3) return 2;
    community::Options o;
    if (!community::ParseAddress(argv[1], o.host, o.port)) return 2;
    o.crossplay = true; o.profileName = argv[2]; o.gameProtocol = "ttt2-port-10-rollback";
    o.loadCredential = server_list::LoadCredential; o.saveCredential = server_list::SaveCredential;
    auto client = std::make_shared<community::Client>(); community::Welcome w; std::string error;
    if (!client->Connect(o,w,error)) { std::fprintf(stderr,"login: %s\n",error.c_str()); return 1; }
    const auto issuedPeer = w.peer;
    client->Disconnect(); client = std::make_shared<community::Client>();
    if (!client->Connect(o,w,error) || w.peer != issuedPeer) { std::fprintf(stderr,"persistent identity reconnect failed\n"); return 1; }
    if (!crossplay::Start(client,w,Receive,error)) { std::fprintf(stderr,"transport: %s\n",error.c_str()); return 1; }
    const uint64_t room = 0x8000000012345678ull;
    const bool host = std::strcmp(argv[2],"host") == 0;
    if (host) {
        std::vector<uint8_t> rec; Put32(rec,0x33534553); Put64(rec,room); Put64(rec,w.peer); Put64(rec,0x9000012345678);
        Put32(rec,0x29); Put32(rec,8); Put32(rec,0); Put32(rec,1); Put32(rec,0);
        const size_t at=rec.size(); rec.resize(at+0x3c); BE(rec,at,room); BE(rec,at+32,w.peer);
        Put32(rec,0); Put32(rec,0); Put32(rec,0x80000001);
        const std::string address = argv[1]; rec.push_back(uint8_t(address.size())); rec.push_back(uint8_t(address.size() >> 8));
        rec.insert(rec.end(), address.begin(), address.end()); rec.push_back(0); rec.push_back(0);
        client->PublishSession(rec);
    } else {
        bool listed = false;
        for (int i=0;i<20 && !listed;++i) {
            listed = !client->ListSessions(20,1000).empty();
            if (!listed) std::this_thread::sleep_for(std::chrono::milliseconds(200));
        }
        for (int i=0;i<150 && !discovered;++i) { Probe(client); std::this_thread::sleep_for(std::chrono::milliseconds(20)); }
        if (!discovered || !client->RoomPeers().empty()) { std::fprintf(stderr,"pre-admission discovery failed\n"); crossplay::Stop(); return 1; }
        Probe(client);
        // These must not escape the discovery-only transport permission.
        const uint8_t forbidden[1024] = {};
        for (const auto& [peer, steam] : client->ConnectionPeers()) {
            crossplay::Send(peer,true,forbidden,sizeof(forbidden));
            crossplay::Send(peer,false,forbidden,sizeof(forbidden));
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        bool joined=false;
        for(int i=0;i<20 && !joined;++i) { joined=client->JoinRoom(room); if(!joined) std::this_thread::sleep_for(std::chrono::milliseconds(200)); }
        if(!joined) { std::fprintf(stderr,"join failed\n"); crossplay::Stop(); return 1; }
    }
    const auto end=std::chrono::steady_clock::now()+std::chrono::seconds(20);
    int sent=0; bool sawRelay=false;
    while(std::chrono::steady_clock::now()<end) {
        sawRelay |= crossplay::RelayedPeers() > 0;
        const auto peers=client->RoomPeers();
        if (peers.empty()) Probe(client);
        if (!peers.empty() && sent<100) {
            uint8_t packet[1024]; packet[0]=uint8_t(sent); packet[1]=uint8_t(sent>>8);
            for(size_t i=2;i<sizeof(packet);++i) packet[i]=uint8_t(sent+i);
            if (crossplay::Send(peers.begin()->first,true,packet,sizeof(packet))) {
                crossplay::Send(peers.begin()->first,false,packet,sizeof(packet)); ++sent;
            }
        }
        if(received>=100 && sent>=100) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    // Keep the connection alive while the other process drains its last messages.
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    const bool relayed = sawRelay || crossplay::RelayedPeers() > 0;
    crossplay::Stop(); client->Disconnect();
    std::printf("%s: sent=%d received=%d unreliable=%d bad=%d relayed=%d discovery=%d\n",argv[2],sent,received.load(),unreliableReceived.load(),int(bad.load()),int(relayed),discovered.load());
    return discovered>0 && received==100 && unreliableReceived>0 && !bad && (!std::getenv("TTT2_CROSSPLAY_FORCE_RELAY") || relayed) ? 0 : 1;
}
