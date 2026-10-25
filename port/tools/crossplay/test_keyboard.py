"""Compile the production keyboard ABI with isolated guest-memory/UI boundaries."""
from pathlib import Path
import os
import re
import subprocess as sp
import tempfile

ROOT = Path(__file__).resolve().parents[3]
KERNEL = ROOT / "port/src/runtime/kernel"
PREFIX = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <functional>
#include <optional>
#include <string>
#include <vector>
uint8_t memory[65536];
struct Reg { uint32_t u32=0; };
struct PPCContext { Reg r1,r3,r4,r5,r6,r7,r8,r9,r10; };
#define PPC_FUNC(name) void name(PPCContext& ctx,uint8_t* base)
void KTrace(const char*) {}
uint32_t GRead32(uint8_t* b,uint32_t p){uint32_t v;std::memcpy(&v,b+p,4);return __builtin_bswap32(v);}
void GWrite32(uint8_t* b,uint32_t p,uint32_t v){v=__builtin_bswap32(v);std::memcpy(b+p,&v,4);}
void GWrite16(uint8_t* b,uint32_t p,uint16_t v){v=__builtin_bswap16(v);std::memcpy(b+p,&v,2);}
namespace host_vm {
bool CanWrite(const void* p,size_t n){auto at=uintptr_t(p),base=uintptr_t(memory);return at>=base&&at-base<sizeof(memory)&&n<=sizeof(memory)-(at-base);}
bool ReadMemory(const void* p,void* out,size_t n){if(!CanWrite(p,n))return false;std::memcpy(out,p,n);return true;}
}
constexpr uint32_t XAM_SUCCESS=0,XAM_INVALID_PARAMETER=87,XAM_NOT_SUPPORTED=50,XAM_INVALID_HANDLE=6,XAM_IO_PENDING=997;
bool XamGuestRange(uint32_t,uint64_t,uint32_t=4);
bool KernelIsEventHandle(uint32_t h){return h==123;}
int signals=0;
bool KernelSignalEventHandle(uint32_t h){assert(h==123);assert(GRead32(memory,0x200)!=997);++signals;return true;}
std::vector<uint32_t> notifications;
void XamNotifyEnqueueEvent(uint32_t id,uint32_t v){assert(id==9);notifications.push_back(v);}
@SYSUI@
std::optional<SysUiTextBox> pending;
bool SysUiShowTextBox(SysUiTextBox box){if(pending)return false;pending=std::move(box);return true;}
bool SysUiShowMessageBox(SysUiMessageBox){return false;}
@HELPERS@
'''
TEST = r'''
void Text(uint32_t at,const std::u16string& s){for(auto c:s){GWrite16(memory,at,c);at+=2;}GWrite16(memory,at,0);}
std::u16string Read(uint32_t at){std::u16string s;for(;;at+=2){uint16_t v;std::memcpy(&v,memory+at,2);v=__builtin_bswap16(v);if(!v)return s;s+=char16_t(v);}}
PPCContext Prepare(uint32_t capacity=32){
 std::memset(memory,0xA5,sizeof(memory));
 for(int i=0;i<28;i+=4)GWrite32(memory,0x200+i,0);
 GWrite32(memory,0x20c,123);Text(0x400,u"hello Ω");Text(0x600,u"Chat");Text(0x800,u"Type a message");
 PPCContext c;c.r3.u32=0;c.r5.u32=0x400;c.r6.u32=0x600;c.r7.u32=0x800;c.r8.u32=0x1000;c.r9.u32=capacity;c.r10.u32=0x200;return c;
}
int main(){
 for(bool accept:{true,false}){
  auto c=Prepare();auto original=c;__imp__XamShowKeyboardUI(c,memory);
  assert(c.r3.u32==997&&pending&&GRead32(memory,0x200)==997);
  assert(pending->text=="hello Ω"&&pending->prompt=="Chat"&&signals==int(!accept));
  auto snapshot=std::vector<uint8_t>(memory,memory+sizeof(memory));
  auto busy=original;__imp__XamShowKeyboardUI(busy,memory);assert(busy.r3.u32==5);
  assert(!std::memcmp(snapshot.data(),memory,sizeof(memory)));
  auto box=std::move(*pending);pending.reset();box.done(accept,"hello 🌍");
  assert(GRead32(memory,0x200)==(accept?0:1223));
  if(accept)assert(Read(0x1000)==u"hello 🌍");else assert(memory[0x1000]==0xA5);
  assert(memory[0xfff]==0xA5&&memory[0x1040]==0xA5);
 }
 assert(signals==2&&notifications==std::vector<uint32_t>({1,0,1,0}));
 for(unsigned capacity:{1,2,3,4}){
  auto c=Prepare(capacity);__imp__XamShowKeyboardUI(c,memory);assert(c.r3.u32==997);
  auto box=std::move(*pending);pending.reset();box.done(true,"🌍a");
  assert(Read(0x1000)==(capacity<3?u"":capacity==3?u"🌍":u"🌍a"));
  assert(memory[0x1000+capacity*2]==0xA5);
 }
 for(int invalid=0;invalid<10;++invalid){
  auto c=Prepare();uint32_t expected=87;
  switch(invalid){
   case 0:c.r3.u32=4;break;case 1:c.r9.u32=0;break;case 2:c.r9.u32=2049;break;
   case 3:c.r8.u32=0x201;break;case 4:c.r8.u32=0x204;break;case 5:c.r8.u32=0xfff0;break;
   case 6:c.r5.u32=0xffff;break;case 7:c.r10.u32=0;expected=50;break;
   case 8:GWrite32(memory,0x210,1);expected=50;break;case 9:GWrite32(memory,0x20c,999);expected=6;break;
  }
  auto snapshot=std::vector<uint8_t>(memory,memory+sizeof(memory));
  __imp__XamShowKeyboardUI(c,memory);assert(c.r3.u32==expected&&!pending);
  assert(!std::memcmp(snapshot.data(),memory,sizeof(memory)));
 }
 std::puts("PASS keyboard ABI: asynchronous completion, cancellation, busy handling, UTF-16/surrogate bounds, invalid pointers");
}
'''


def main():
    production = re.sub(r'^#include "[^\n]+"\n', '', (KERNEL / "kernel_ui.cpp").read_text(), flags=re.M)
    content = (KERNEL / "kernel_content.cpp").read_text()
    helpers = content[content.index("bool XamGuestRange("):content.index("bool TryCloseContentEnumerator(")]
    sysui = (KERNEL / "sys_ui.h").read_text().replace("#pragma once", "")
    source = PREFIX.replace("@SYSUI@", sysui).replace("@HELPERS@", helpers) + production + TEST
    with tempfile.TemporaryDirectory(prefix="ttt2-keyboard-") as temp:
        path = Path(temp) / "test.cpp"; path.write_text(source)
        binary = Path(temp) / "test"
        sp.run([os.environ.get("CXX", "c++"), "-std=c++20", "-pthread", "-fsanitize=address,undefined", str(path), "-o", str(binary)], check=True)
        sp.run([str(binary)], check=True)


if __name__ == "__main__":
    main()
