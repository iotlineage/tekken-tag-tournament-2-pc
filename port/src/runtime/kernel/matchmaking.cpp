// Session discovery and invitations across Steam, community and local backends.
#include "host_settings.h"
#include "matchmaking.h"
#include "community_net.h"
#include "net_transport.h"
#include "steam_net.h"
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <deque>
#include "server_list.h"
#include <map>
#include <set>
#include <mutex>
#include <thread>

namespace mm {
namespace {

constexpr uint32_t kMagic = 0x32534553u;       // "SES2"
constexpr int kStaleSeconds = 10;

std::mutex g_mutex;
std::map<uint64_t, SessionRecord> g_owned;
std::map<uint64_t, SessionRecord> g_seen;       // listed or invited sessions, for Find
bool g_heartbeat = false;
std::deque<SessionRecord> g_invites;
std::set<uint64_t> g_socialRooms;

bool Steam() { return std::strcmp(net::BackendName(), "steam") == 0; }
std::shared_ptr<community::Client> Server() { return net::Community(); }

std::filesystem::path Folder()
{
    return host_settings::Path().parent_path() / "net" / "sessions";
}
std::filesystem::path FileOf(uint64_t id)
{
    wchar_t name[32];
    swprintf(name, 32, L"%016llX.ses", static_cast<unsigned long long>(id));
    return Folder() / name;
}

void Put32(std::vector<uint8_t>& v, uint32_t x) { for (int i = 0; i < 4; ++i) v.push_back(uint8_t(x >> (8 * i))); }
void Put64(std::vector<uint8_t>& v, uint64_t x) { Put32(v, uint32_t(x)); Put32(v, uint32_t(x >> 32)); }

std::vector<uint8_t> Serialize(const SessionRecord& r)
{
    std::vector<uint8_t> v;
    Put32(v, r.crossplay ? 0x33534553u : kMagic);
    Put64(v, r.id);
    Put64(v, r.hostPeer);
    Put64(v, r.hostXuid);
    Put32(v, r.flags);
    Put32(v, r.maxPublic); Put32(v, r.maxPrivate); Put32(v, r.filledPublic); Put32(v, r.filledPrivate);
    v.insert(v.end(), r.info, r.info + sizeof(r.info));
    Put32(v, uint32_t(r.contexts.size()));
    for (const Context& c : r.contexts) { Put32(v, c.id); Put32(v, c.value); }
    Put32(v, uint32_t(r.properties.size()));
    for (const Property& p : r.properties)
    {
        Put32(v, p.id);
        Put32(v, uint32_t(p.value.size()));
        v.insert(v.end(), p.value.begin(), p.value.end());
    }
    Put32(v, r.netcode);                       // last: records without it read as 0
    if (r.crossplay) {
        for (const auto& text : {r.serverAddress, r.roomCode}) {
            const uint16_t n = uint16_t(text.size()); v.push_back(uint8_t(n)); v.push_back(uint8_t(n >> 8));
            v.insert(v.end(), text.begin(), text.end());
        }
    }
    return v;
}

bool Deserialize(const std::vector<uint8_t>& v, SessionRecord& r)
{
    size_t at = 0;
    auto get32 = [&](uint32_t& x) {
        if (at + 4 > v.size()) return false;
        x = uint32_t(v[at]) | uint32_t(v[at + 1]) << 8 | uint32_t(v[at + 2]) << 16 | uint32_t(v[at + 3]) << 24;
        at += 4;
        return true;
    };
    auto get64 = [&](uint64_t& x) { uint32_t lo, hi; if (!get32(lo) || !get32(hi)) return false; x = uint64_t(hi) << 32 | lo; return true; };
    uint32_t magic = 0, n = 0;
    if (!get32(magic) || (magic != kMagic && magic != 0x33534553u) || !get64(r.id) || !get64(r.hostPeer) || !get64(r.hostXuid) || !get32(r.flags) ||
        !get32(r.maxPublic) ||
        !get32(r.maxPrivate) || !get32(r.filledPublic) || !get32(r.filledPrivate) || at + sizeof(r.info) > v.size())
        return false;
    std::memcpy(r.info, v.data() + at, sizeof(r.info));
    at += sizeof(r.info);
    if (!get32(n) || n > 64) return false;
    r.contexts.resize(n);
    for (Context& c : r.contexts) if (!get32(c.id) || !get32(c.value)) return false;
    if (!get32(n) || n > 64) return false;
    r.properties.resize(n);
    for (Property& p : r.properties)
    {
        uint32_t size = 0;
        if (!get32(p.id) || !get32(size) || size > 4096 || at + size > v.size()) return false;
        p.value.assign(v.begin() + at, v.begin() + at + size);
        at += size;
    }
    r.netcode = 0;
    if (at + 4 <= v.size()) get32(r.netcode);
    r.crossplay = magic == 0x33534553u;
    r.serverAddress.clear();
    r.roomCode.clear();
    if (r.crossplay) {
        for (auto* text : {&r.serverAddress, &r.roomCode}) {
            if (at + 2 > v.size()) return false;
            const size_t n = v[at] | size_t(v[at+1]) << 8; at += 2;
            if (n > (text == &r.serverAddress ? 512u : 32u) || at + n > v.size()) return false;
            text->assign(reinterpret_cast<const char*>(v.data()+at), n); at += n;
        }
        std::string host; uint16_t port;
        if (!community::ParseAddress(r.serverAddress, host, port)) return false;
    }
    return at == v.size();
}

void Write(const SessionRecord& r)
{
    std::error_code ec;
    std::filesystem::create_directories(Folder(), ec);
    const std::vector<uint8_t> bytes = Serialize(r);
    const std::filesystem::path file = FileOf(r.id), temp = file.wstring() + L".tmp";
    {
        std::ofstream out(temp, std::ios::binary | std::ios::trunc);
        out.write(reinterpret_cast<const char*>(bytes.data()), std::streamsize(bytes.size()));
    }
    std::filesystem::rename(temp, file, ec);
}

void Heartbeat()
{
    for (;;)
    {
        std::this_thread::sleep_for(std::chrono::seconds(2));
        std::lock_guard l(g_mutex);
        if (!Server() && !Steam()) for (const auto& [id, r] : g_owned) Write(r);
    }
}

bool Fresh(const std::filesystem::path& file)
{
    std::error_code ec;
    const auto t = std::filesystem::last_write_time(file, ec);
    if (ec) return false;
    return std::filesystem::file_time_type::clock::now() - t < std::chrono::seconds(kStaleSeconds);
}

bool Load(const std::filesystem::path& file, SessionRecord& r)
{
    std::ifstream in(file, std::ios::binary);
    if (!in) return false;
    std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    return Deserialize(bytes, r);
}

} // namespace

void Publish(const SessionRecord& record)
{
    std::lock_guard l(g_mutex);
    g_owned[record.id] = record;
    if (const std::shared_ptr<community::Client> server = Server()) {
        SessionRecord advertised = record;
        advertised.crossplay = server->Crossplay();
        advertised.serverAddress = net::ServerAddress();
        server->PublishSession(Serialize(advertised));
        if (server->Crossplay() && steamnet::Running()) {
            if (server->RoomCode(record.id).empty()) server->JoinRoom(record.id);
            advertised.roomCode = server->RoomCode(record.id);
            if (!advertised.roomCode.empty()) {
                steamnet::PublishLobby(record.id, Serialize(advertised));
                g_socialRooms.insert(record.id);
            }
        }
    }
    else if (Steam()) steamnet::PublishLobby(record.id, Serialize(record));
    else
    {
        Write(record);
        if (!g_heartbeat) { g_heartbeat = true; std::thread(Heartbeat).detach(); }
    }
    std::fprintf(stderr, "[mm] published %016llX (slots %u/%u)\n", (unsigned long long)record.id,
                 record.filledPublic, record.maxPublic);
}

void Remove(uint64_t id)
{
    std::lock_guard l(g_mutex);
    steamnet::RemoveLobby(id);
    g_socialRooms.erase(id);
    if (!g_owned.erase(id)) return;
    if (const std::shared_ptr<community::Client> server = Server()) { server->RemoveSession(id); std::fprintf(stderr, "[mm] removed %016llX\n", (unsigned long long)id); return; }
    std::error_code ec;
    std::filesystem::remove(FileOf(id), ec);
    std::fprintf(stderr, "[mm] removed %016llX\n", (unsigned long long)id);
}

std::vector<SessionRecord> List(uint32_t max, const std::vector<std::pair<uint32_t, uint32_t>>& contexts, bool openOnly)
{
    if (const std::shared_ptr<community::Client> server = Server())
    {
        std::vector<SessionRecord> out;
        for (const std::vector<uint8_t>& bytes : server->ListSessions((std::max)(max, 1u), 5000, contexts, openOnly))
        {
            SessionRecord r;
            if (out.size() >= max || !Deserialize(bytes, r) || r.crossplay != server->Crossplay() || r.hostPeer == net::LocalPeer()) continue;
            {
                std::lock_guard l(g_mutex);
                g_seen[r.id] = r;
            }
            out.push_back(std::move(r));
        }
        return out;
    }
    if (Steam())
    {
        std::vector<SessionRecord> out;
        for (const std::vector<uint8_t>& bytes : steamnet::ListLobbies(int((std::max)(max, 1u) * 2), 5000))
        {
            SessionRecord r;
            // TTT2_MM_LIST_OWN=1 (diagnostics): keep this account's own lobbies, so a second
            // instance on the same Steam account can check lobby discovery.
            static const bool listOwn = std::getenv("TTT2_MM_LIST_OWN") != nullptr;
            if (out.size() >= max || !Deserialize(bytes, r) || (r.hostPeer == net::LocalPeer() && !listOwn)) continue;
            {
                std::lock_guard l(g_mutex);
                g_seen[r.id] = r;
            }
            out.push_back(std::move(r));
        }
        return out;
    }
    std::vector<std::pair<std::filesystem::file_time_type, SessionRecord>> found;
    std::error_code ec;
    for (const auto& entry : std::filesystem::directory_iterator(Folder(), ec))
    {
        if (entry.path().extension() != L".ses" || !Fresh(entry.path())) continue;
        SessionRecord r;
        if (!Load(entry.path(), r) || r.hostPeer == net::LocalPeer()) continue;
        found.emplace_back(entry.last_write_time(ec), std::move(r));
    }
    std::sort(found.begin(), found.end(), [](const auto& a, const auto& b) { return a.first > b.first; });
    std::vector<SessionRecord> out;
    for (auto& f : found) { if (out.size() >= max) break; out.push_back(std::move(f.second)); }
    return out;
}

bool Find(uint64_t id, SessionRecord& out)
{
    SessionRecord cached;
    bool haveCached = false;
    {
        std::lock_guard l(g_mutex);
        const auto it = g_seen.find(id);
        if (it != g_seen.end()) { cached = it->second; haveCached = true; }
    }
    if (const auto server = Server()) {
        std::vector<uint8_t> bytes;
        if (!server->GetSession(id, bytes, 2000) || !Deserialize(bytes, out) || out.id != id ||
            out.crossplay != server->Crossplay()) return false;
        // Invite metadata supplies only the capability; the current server owns the room record.
        if (haveCached && cached.hostPeer == out.hostPeer && cached.crossplay == out.crossplay &&
            server_list::SameAddress(cached.serverAddress, net::ServerAddress())) out.roomCode = cached.roomCode;
        out.serverAddress = net::ServerAddress();
        std::lock_guard l(g_mutex);
        g_seen[id] = out;
        return true;
    }
    if (Steam()) { if (!haveCached || cached.crossplay) return false; out = cached; return true; }
    const auto file = FileOf(id);
    return Fresh(file) && Load(file, out);
}

void HideSocialRooms() {
    std::lock_guard l(g_mutex);
    for (auto id : g_socialRooms) steamnet::RemoveLobby(id);
    g_socialRooms.clear();
}
void ClearSeen() {
    HideSocialRooms();
    std::lock_guard l(g_mutex); g_seen.clear(); g_invites.clear();
}
void AdvertiseInvite(const SessionRecord& record) {
    std::lock_guard l(g_mutex);
    if (record.crossplay && !record.roomCode.empty() && steamnet::Running()) {
        steamnet::PublishLobby(record.id, Serialize(record));
        g_socialRooms.insert(record.id);
    }
}
void QueueInvite(const SessionRecord& record) {
    std::lock_guard l(g_mutex); if (g_invites.size() < 8) g_invites.push_back(record);
}
bool TakeSteamInvite(SessionRecord& out) {
    std::vector<uint8_t> bytes;
    return steamnet::TakeJoinRequest(bytes) && Deserialize(bytes, out);
}
bool TakeInvite(SessionRecord& out)
{
    if (const auto server = Server()) {
        std::vector<uint8_t> bytes;
        uint64_t from = 0;
        SessionRecord invited;
        if (server->TakeInvite(bytes, from) && Deserialize(bytes, invited) &&
            invited.crossplay == server->Crossplay()) {
            if (invited.crossplay) invited.serverAddress = net::ServerAddress();
            QueueInvite(invited);
        }
    }
    if (const auto disabled = std::getenv("TTT2_SRV_UI"); disabled && *disabled == '0') {
        SessionRecord invited;
        if (TakeSteamInvite(invited) && !invited.crossplay && !Server()) QueueInvite(invited);
    }
    std::lock_guard l(g_mutex);
    if (g_invites.empty()) return false;
    out = std::move(g_invites.front()); g_invites.pop_front();
    if (out.crossplay != net::Crossplay() || (out.crossplay &&
        !server_list::SameAddress(out.serverAddress, net::ServerAddress()))) return false;
    g_seen[out.id] = out;
    return true;
}

} // namespace mm
