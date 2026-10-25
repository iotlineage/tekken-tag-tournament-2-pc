"""Exercise production XUID/XNADDR code across network changes with isolated guest RAM."""
from pathlib import Path
import os
import subprocess as sp
import tempfile

source = (Path(__file__).resolve().parents[3] / 'port/src/runtime/kernel/xlive.cpp').read_text()
def function(signature):
    start = source.index(signature)
    at = source.index('{', start)
    depth = 1
    end = at + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]
prefix = r'''
#include <array>
#include <map>
#include <mutex>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cassert>
uint8_t memory[65536]{};
namespace net { uint64_t peer=0x4c4f4f5000006dc4ull; uint64_t LocalPeer(){return peer;} uint64_t ProfilePeer(){return 0x4c4f4f5000006dc4ull;} }
namespace profiles { bool SignedIn(){return true;} uint64_t ActiveXuid(){return 1234;} }
void XSocketStartTransport(){}
void GWrite32(uint8_t* b,uint32_t a,uint32_t v){v=__builtin_bswap32(v);std::memcpy(b+a,&v,4);}
void GWrite16(uint8_t* b,uint32_t a,uint16_t v){v=__builtin_bswap16(v);std::memcpy(b+a,&v,2);}
void GWrite8(uint8_t* b,uint32_t a,uint8_t v){b[a]=v;}
void Write64(uint8_t* b,uint32_t a,uint64_t v){v=__builtin_bswap64(v);std::memcpy(b+a,&v,8);}
uint64_t Read64(uint8_t* b,uint32_t a){uint64_t v;std::memcpy(&v,b+a,8);return __builtin_bswap64(v);}
uint32_t OwnAddr(uint64_t p){return 0x0a4d0000 | uint32_t(p&65535);}
std::mutex g_titleAddrMutex;
std::map<uint32_t,std::array<uint8_t,0x24>> g_titleAddrs;
void KTrace(const char*){}
template<class... A> void Trace(A...){}
struct Reg { union { uint64_t u64=0; uint32_t u32; }; };
struct PPCContext { Reg r3,r4; uint64_t lr=0; };
#define PPC_FUNC(name) void name(PPCContext& ctx,uint8_t* base)
namespace xlive { bool Enabled(){return true;} uint64_t LocalPeerId(){return net::LocalPeer();}
'''
code = prefix + '\n'.join(function(x) for x in ['uint64_t OnlineXuid(', 'void WriteOwnXnAddr(', 'void RefreshTitleXnAddr(']) + '\n}\n'
code += function('PPC_FUNC(__imp__NetDll_XNetGetTitleXnAddr)')
code += r'''
int main(){
 PPCContext c;
 c.lr=0x82B3B9F4; c.r4.u32=0x100; __imp__NetDll_XNetGetTitleXnAddr(c,memory);
 c.lr=0x82B545BC; c.r4.u32=0x200; __imp__NetDll_XNetGetTitleXnAddr(c,memory);
 c.lr=0x828CADE4; c.r4.u32=0x300; __imp__NetDll_XNetGetTitleXnAddr(c,memory);
 assert(g_titleAddrs.size()==2);
 const auto old=net::peer, xuid=xlive::OnlineXuid(0);
 net::peer=0x4350123456789012ull;
 xlive::RefreshTitleXnAddr(memory);
 assert(Read64(memory,0x118)==net::peer && Read64(memory,0x218)==net::peer);
 assert(Read64(memory,0x318)==old); // stack result must not be retained/reused
 assert(xlive::OnlineXuid(0)==xuid);
 assert(xlive::OnlineXuid(1)!=xuid);
 memory[0x100]=0x77; // guest freed/reused the persistent result
 net::peer=old;
 xlive::RefreshTitleXnAddr(memory);
 assert(memory[0x100]==0x77 && Read64(memory,0x118)!=old);
 assert(Read64(memory,0x218)==old && g_titleAddrs.size()==1);
 assert(xlive::OnlineXuid(0)==xuid);
 puts("PASS stable XUID, refreshed persistent XNADDR, stack/reused buffers untouched");
}
'''
with tempfile.TemporaryDirectory(prefix='ttt2-identity-test-') as tmp:
    cpp, exe = Path(tmp)/'test.cpp', Path(tmp)/'test'
    cpp.write_text(code)
    sp.run([os.environ.get('CXX','clang++'),'-std=c++20','-fsanitize=address,undefined','-g',str(cpp),'-o',str(exe)],check=True)
    sp.run([str(exe)],check=True)
