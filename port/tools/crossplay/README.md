# Cross-platform networking

`TTT2_CROSSPLAY` adds standalone GameNetworkingSockets (GNS), separately from Steamworks. The default is OFF so existing build environments keep working. Every participant in a cross-platform room needs an enabled build. Steam remains optional and runs in the existing helper process.

## Dependencies and builds

Use Python 3, Git, CMake, Ninja and a C++ toolchain. Native builds additionally need OpenSSL development headers. The helper pins GNS and protobuf, enables native ICE/TURN, applies the Android local-address patch, and installs licenses. Keep dependency caches and build outputs outside this checkout.

```sh
python3 port/tools/crossplay/build_dependencies.py \
  --target native --prefix /path/deps/native --cache /path/cache/native
cmake -S port -B /path/game-build [existing game build options] \
  -DTTT2_CROSSPLAY=ON -DCMAKE_PREFIX_PATH=/path/deps/native
cmake --build /path/game-build
```

For the native Linux game, build the explicit `tekken_recomp_linux` target (`cmake --build /path/game-build --target tekken_recomp_linux`); it is excluded from the default build. Retain `TTT2_LINUX_STEAM=ON` and the Steamworks SDK path when Steam social features are wanted.

For Windows, use `--target windows` from a Visual Studio developer shell; or pass `--toolchain` and `--cmake-arg=-DTTT2_WINSDK=...` for the Linux-hosted clang-cl toolchain. Windows uses BCrypt for GNS and SChannel for control TLS. Pass a host `--protoc /path/deps/native/bin/protoc` when cross-compiling. All protobuf/Abseil/GNS components must use the same target toolchain, runtime and language ABI. The helper enables `/Zc:__cplusplus` to keep Abseil's installed configuration consistent with its build.

For Android ARM64:

```sh
python3 port/tools/crossplay/build_dependencies.py --target android \
  --toolchain "$ANDROID_NDK/build/cmake/android.toolchain.cmake" \
  --openssl /path/android-openssl --protoc /path/deps/native/bin/protoc \
  --prefix /path/deps/android --cache /path/cache/android
```

Add `-DTTT2_CROSSPLAY=ON -DCMAKE_PREFIX_PATH=/path/deps/android` and `-DCMAKE_FIND_ROOT_PATH=/path/deps/android` to the existing `port/android/game` configuration. Retain its `TTT2_OPENSSL_ROOT`. The Android patch uses `getifaddrs` where available and a default-route socket lookup when Android blocks interface enumeration. Stock GNS at the pinned revision has no Android local-address implementation; an unpatched build can compile but cannot establish ICE connections.

## Playing

Select **ONLINE MODE > SERVER > STEAM** for Steam-only rooms, or a crossplay-enabled server for cross-platform rooms. This selects the type before room creation; an active room cannot change type. Cross-platform records use SES3 and are excluded from legacy/Steam-only searches. Existing legacy community servers remain available through their advertised mode; `TTT2_SERVER_LEGACY=1` forces legacy mode for diagnostics.

Within a cross-platform room, **FRIEND INVITE** offers **COPY ROOM CODE** or **STEAM FRIEND**. Send the copied code to a friend, who selects **ONLINE MODE > SERVER > JOIN ROOM CODE**. Codes contain the server address, session ID and an unguessable room capability; share them only with intended participants. The server still enforces room capacity and existence. Deleting/recreating a room invalidates its old capability.

Steam lobby metadata includes the community address and room capability. Accepting an invitation connects to that community, authenticates, fetches its current room record, and then requests admission. Password-protected communities still ask for their password. Launch arguments `+connect_lobby <id>` and running-client join callbacks use the same flow. On App ID 480, Steam may launch Spacewar or the configured Steam shortcut instead of the port; the port can process the argument only if Steam actually launches this executable.

Match-start reporting calls Steam's `SetPlayedWith` through the helper for the actual opponent. Cross-platform reporting uses only server-verified Steam bindings; non-Steam participants and room browsers are excluded. The server needs a Steam Web API key for these bindings. The App ID 480 game association and Steam's history UI require the two-account manual check below.

Android stores account credentials and their encryption key in its internal app files directory. Game saves and the server list remain in external app storage; that emulated filesystem cannot enforce the POSIX permissions required by the account key.

The original keyboard request now completes asynchronously with UTF-16 input or cancellation, enabling the title's original chat submission path. It uses the port's existing text box and original game packets. End-to-end chat display and delivery remain part of gameplay verification.

The TU3 static trace identifies `CPlayerMatchTextChatTask::Impl` and `CSendVerifiedChatMessageTask` in the original image. `sub_8269BB88` requests input through `sub_8269DB20`; `sub_8269CBB8` calls the keyboard import and `sub_8269CAF8` copies the completed UTF-16 result. `sub_8269BE00` then creates the send task (`sub_8269C368`). Its update (`sub_8269C6C0`) verifies the text through the existing `XStringVerify` implementation and dispatches the resulting message to active room peers via `sub_826C4920`. The original message serializer/deserializer are `sub_826BA998` / `sub_826BAA50`. This path retains the game's packet format and uses the selected transport; it does not need a separate community chat protocol. This is source/binary trace evidence, not a live delivery test.


## Repeatable checks

```sh
go -C server test -race ./...
python3 port/tools/crossplay/test_keyboard.py
go -C server build -o /path/ttt2-server ./cmd/ttt2-server
cmake -S port/tools/crossplay -B /path/probe -G Ninja \
  -DCMAKE_PREFIX_PATH=/path/deps/native
cmake --build /path/probe
python3 port/tools/crossplay/smoke.py --server /path/ttt2-server \
  --probe /path/probe/crossplay_probe
# Requires Docker; starts and removes its own coturn container.
python3 port/tools/crossplay/smoke.py --server /path/ttt2-server \
  --probe /path/probe/crossplay_probe --relay
```

Build the same probe with the Android or Windows toolchain and target dependencies. Add `--android-probe /path/android/crossplay_probe --serial DEVICE` to test a physical device against Linux. Both must be able to reach the host's LAN address. Add `--guest-probe /path/windows/crossplay_probe.exe` for a Windows binary under Wine. Wine is a useful transport check, not a replacement for native Windows gameplay testing. The harness uses isolated temporary accounts and cleans up its processes, container and device files. It sends 100 ordered reliable 1 KiB messages and 100 unreliable messages each way, checks payload integrity, reconnects using a persisted account, and requires GNS to report a relay when forced.

Use `TTT2_CROSSPLAY_FORCE_RELAY=1` for an entire test run to disable direct candidates. `TTT2_CROSSPLAY_NETLOG=1` enables verbose GNS diagnostics. Neither is needed for ordinary play.

## Manual acceptance checklist

- With the community service unavailable, create Steam-only rooms; fight, spectate, chat, leave and rejoin.
- Create cross-platform rooms across native Windows, Linux and Android, including Steam/non-Steam mixes. Check fights, spectators, chat, private slots and password prompts, on direct and forced-relay routes.
- Interrupt the control connection and a peer connection. Confirm failure stays within the current room type; leave, reconnect with the same account, and create/join a new room. There is no host migration.
- Accept stale/full-room invitations, wrong room codes, incompatible room types, and an invitation while already in a room. Switch types after leaving and confirm no prior peers remain reachable.
- With two different Steam accounts, test running-game **Join Game** and **Invite to Game**, and launch-time `+connect_lobby`, for both types. Verify the current host's room disappears when closed.
- On both Steam clients, inspect **Recently Played With** after a real fight in each type. Include a non-Steam spectator in the cross-platform test; verify exclusion and the actual App ID 480 / Spacewar association. The user has confirmed these two-account tests are unavailable locally and must remain manual.
- Measure actual game/control/relay traffic during fights and spectator sessions before sizing a public deployment.

Live-test fixes (2026-10-25): the title searches by sending UDP 10000 probes
before XSessionCreate/ROOM_JOIN. Public listings grant a bounded 30-second
discovery lease, allowing only those datagrams until admission. The probe now
exercises discovery in both directions before joining and checks that unrelated
packets are blocked. Linux/physical Android direct and Linux/Linux forced TURN
tests pass with ordered reliable and unreliable payloads after admission.

Online XUIDs retain the base profile transport seed when the routing identity
switches to a server account. Previously the first GIN login cached one XUID,
and a later licence upload used a different current XUID and failed ownership
validation, producing the title's communication error. With the updated clients,
both Linux and Android completed their first online entry and stored the licence.
Android discovered and joined Linux's Player Match room; the host logged
XSessionJoinRemote and XSessionStart with live cross-platform packets. Full fight,
spectator, chat and two-account Steam social acceptance remain separate checks.

Fight-start routing regression: the title caches its own XNADDR before entering
Online Mode (persistent result at the 0x82B3B9F4 call site, plus the global
0x82B545BC call site). Its later NACE handshake used the original loopback/Steam
peer even though the community session used a native peer; actual Linux traces
showed incoming Android traffic on 10.78.0.2 and outgoing fight traffic addressed
to the stale loopback peer on 10.78.0.3. Refresh these known persistent results on
the game's main thread after a network change. Stack results are not retained,
and a result whose bytes have changed is discarded rather than overwritten.
`python port/tools/crossplay/test_network_identity.py` runs the production
identity/cache functions under ASan/UBSan and covers stable profile identity,
server entry/exit, stack results, and guest-reused result buffers. Linux, Android
and Windows builds pass. With the installed fix, Android hosted and Linux joined;
both refreshed the cached XNADDR, passed syncing and advanced beyond 1,200 fight
frames without the previous startup timeout. Full match completion remains to
be confirmed; Android frame-time/performance is a separate validation item.
