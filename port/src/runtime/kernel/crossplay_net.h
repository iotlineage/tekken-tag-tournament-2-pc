#pragma once
#include "community_net.h"
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>

namespace crossplay {
using Receiver = void (*)(uint64_t, bool, const uint8_t*, size_t);
bool Available();
int RelayedPeers();
bool Start(std::shared_ptr<community::Client>, const community::Welcome&, Receiver, std::string& error);
void Stop();
bool Send(uint64_t peer, bool reliable, const void*, size_t);
}
