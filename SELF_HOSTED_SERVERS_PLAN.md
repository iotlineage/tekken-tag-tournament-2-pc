# Self-hosted servers

## Implementation details

The whole self-hosted server setup will be done in a new worktree. We don't need multiple worktrees.
We will keep the original implementation as intact as possible so we don't need to replace it.
We will control the settings of the new implementation using the existing settings that the original server provides already.

## Answered questions

1. **Can Steam and non-Steam players share a session?** Yes. Cross-platform sessions will use a shared GameNetworkingSockets transport on Windows, Linux, and Android. All participants, including Steam users, connect to the community service for authentication, room membership, and connection setup. Existing Steam-only clients must be updated to join these sessions. The existing local transport is localhost-only and will remain available for testing.

2. **Can we keep Steam invitations and other Steam functionality while using the community server?** Yes. Steam integration will run independently of the gameplay transport when Steam is available. Steam lobbies and invitations will identify the community server and room, allowing Steam friends to join cross-platform sessions. Non-Steam players will join through community discovery or a room code. Preserve existing Steam functionality and support friends, presence, and invitations in cross-platform mode; this does not imply adding an in-game overlay, achievements, or Steam Cloud. The existing "Join Game" path and currently unverified "Invite to Game" behavior both need validation.

3. **Can some sessions use only Steam to reduce load on the community server?** Yes. The host selects one of two session types when creating a room:

   - **Steam-only:** Steam identities, lobby discovery, invitations, and gameplay transport, with Steam relay support. Session networking must work without the community service.
   - **Cross-platform:** Community identities and room coordination, with the shared gameplay transport for every participant. Steam social integration remains optional. Prefer direct peer connections and use self-hosted relay fallback when needed.

   The session type is fixed for the room's lifetime and is included in listings and invitations. Changing it initially requires leaving and recreating the room. Cross-platform rooms do not mix Steam and community gameplay transports per peer. Most community infrastructure bandwidth will come from relayed gameplay; direct connections keep gameplay traffic off the community server.

4. **Can players see previous opponents in Steam's "Recently Played With" history?** Yes. Add reporting through the Steam helper using `ISteamFriends::SetPlayedWith` while players are in-game together. Support both Steam-only sessions and Steam participants in cross-platform sessions, using verified Steam identities. Non-Steam players cannot be reported as Steam accounts. The port currently uses App ID 480, so the recorded game association is expected to be Spacewar; verify the actual history with two Steam accounts. This feature does not require adding an in-game overlay. See [Valve's SetPlayedWith documentation](https://partner.steamgames.com/doc/api/ISteamFriends#SetPlayedWith).

## Implementation steps

- [x] Add Steam-only and cross-platform room types, selected at room creation and fixed for the room's lifetime. Include the type in listings and invitations, and require room recreation to change it.
- [x] Move gameplay transport selection from process startup to the online session lifecycle, with cleanup on leaving and safe selection for the next session. Preserve the existing Steam transport and localhost testing backend.
- [x] Decouple the optional Steam helper, lobby discovery, invitations, and presence from gameplay transport selection.
- [x] Keep Steam-only discovery, identities, invitations, and gameplay independent of the community service; reject incompatible joins with a clear explanation.
- [x] Add a shared GameNetworkingSockets transport for Windows, Linux, and Android. Use it for every participant in a cross-platform room, including Steam users.
- [x] Add server-issued player identities and authentication that do not require Steam. Keep community player identities distinct from Steam IDs and associate them explicitly where needed.
- [x] Implement community room discovery, membership, room codes, and peer connection signaling, reusing any existing service components that fit.
- [x] Add self-hosted STUN and TURN services for direct connections and relay fallback. Verify the selected GameNetworkingSockets build supports the required ICE/TURN path on each target platform.
- [x] Connect cross-platform room discovery, matches, spectators, and reconnects to the community identities and shared transport.
- [x] Publish community server and room references through Steam lobby metadata or rich presence. Resolve Steam joins and invitations through community authentication and room admission, and keep room availability synchronized.
- [x] Add a Steam helper command for `ISteamFriends::SetPlayedWith` and report actual match participants while they are in-game together in either session type. Use verified Steam IDs, skip the local player and non-Steam participants, and avoid reporting players merely discovered in room listings.
- [x] Trace the original session chat and implement its missing text input and message delivery for both session types.
- [ ] Verify Steam-only rooms, fights, spectators, chat, and reconnects with the community service unavailable.
- [ ] Verify cross-platform rooms, fights, spectators, chat, and reconnects across Windows, Linux, and Android, both without Steam and with mixed Steam/non-Steam participants.
- [ ] Verify Steam "Join Game" and "Invite to Game" for both session types, including joining while the game is running and launch-time invitation handling.
- [ ] Verify Steam "Recently Played With" history with two Steam accounts in both session types, including a cross-platform room containing non-Steam players. Check reporting on each Steam client, exclusion of non-Steam players, and the displayed App ID 480 / Spacewar association.
- [ ] Verify room-type enforcement, stale invitation handling, and switching session types after leaving a room without leaking connections or identities.
- [ ] Verify direct connections and forced relay fallback, measure community-server and relay traffic, and reject unauthenticated or unauthorized peer traffic.
- [x] Package the self-hosted service, STUN/TURN configuration, and game settings. Document both session types, optional Steam integration, and relay bandwidth requirements.

## Implementation evidence (2026-10-25)

Worktree: `codex/self-hosted-servers`, based on Linux PR #47 head
`c2d1265bf569e2c72581c2a2ed4a08bb53db14f5`. The original checkout was left intact;
Linux and Android test builds were subsequently installed for the live checks below. Checked implementation items above
refer to code; gameplay acceptance remains separate below.

- Windows, Linux and Android game builds link with the optional transport.
- Real TLS + GNS probes pass Linux/Linux and Linux/physical Android direct and
  forced coturn relay; the Windows executable also passes against Linux under
  Wine for both routes. Each sends 100 ordered reliable 1 KiB messages plus an
  unreliable stream in both directions, verifies payloads, and reconnects with
  the same persisted account. Forced tests require GNS to report a relay.
- The Go server suite passes with the race detector, including native accounts,
  cross-server credential rejection, verified Steam bindings, mode isolation,
  private capabilities, room capacity and signaling authorization.
- The keyboard ABI is implemented and passes an address/undefined-sanitizer
  harness for pending completion, cancellation, busy UI, guest-pointer checks
  and UTF-16/surrogate bounds. The original chat input, verification and peer
  message serialization/dispatch are traced and preserved; actual delivery and
  display still need an end-to-end gameplay check (trace in the build/test guide).
- A fresh pinned dependency build and its forced-relay probe pass. The server
  container builds, and generated deployment configuration passes secret, NAT
  mapping, permission and overwrite checks; Compose configuration validates.
- The pinned GNS dependency needs the included Android address-discovery patch;
  an unpatched build compiled but failed the device connection test.

Still pending: live fights/spectators/chat, network-loss recovery in the actual
title, native Windows runtime checks, and gameplay bandwidth measurements.
The user confirmed two Steam accounts are unavailable: Steam invitation UI,
launch behavior, and Recently Played With are explicitly manual acceptance
checks, not claimed as passed. See [build/test instructions](port/tools/crossplay/README.md)
and [deployment instructions](server/deploy/crossplay/README.md).

Live testing (2026-10-25): installed rebuilt Android APK and Linux binary, and
restarted the local server with authenticated pre-admission discovery leases.
Fixed first Online Mode entry by keeping the profile XUID stable across server
connections. Both clients completed first entry; Android found and joined the
Linux Player Match room, and XSessionStart was observed. This confirms discovery
and lobby admission; it does not yet certify a completed fight or spectator/chat
acceptance. The updated smoke test also checks discovery before room admission
and rejects non-discovery packets before membership.

Fight-start follow-up: fixed the title's pre-login XNADDR cache carrying the old
Steam/loopback peer into NACE. Updated both installed clients; Android-hosted
crossplay passed syncing and both sides advanced beyond 1,200 fight frames.
The stable-identity/cache regression passes ASan/UBSan; Linux, Android and
Windows rebuilds pass. Full completion and performance remain live checks.
