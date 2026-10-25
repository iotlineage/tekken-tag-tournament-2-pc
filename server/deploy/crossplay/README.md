# Self-hosted cross-platform rooms

The community service handles TLS authentication, rooms, signaling and existing game services. Gameplay travels directly between peers when possible. Coturn relays encrypted gameplay when direct ICE fails; it also provides STUN. Steam-only sessions use neither service.

This Compose deployment uses Linux host networking. Create configuration on the host:

```sh
python3 configure.py --hostname play.example.net --public-ip 203.0.113.10
# Behind a router, also pass --bind-ip with this machine's LAN address.
sudo chown -R 65532:65532 local
# The config files remain mode 0600, directory mode 0700.
docker compose up -d --build
docker compose logs -f
```

Replace the example hostname and documentation IP with real addresses. Point DNS at the public IP. Forward/open TCP and UDP 28200, UDP 3478, and UDP 49160–49260. Coturn's TCP listener on 3478 is not required by the native ICE transport. The admin web panel remains disabled unless separately configured. Configure a single advertised STUN endpoint and a single TURN endpoint, both as `hostname:port`; do not add URI prefixes or comma-separated lists.

The generator refuses to overwrite existing configuration. `local/` is ignored by Git and contains secrets. Keep `turn_secret` and coturn's `static-auth-secret` identical. The server issues 24-hour coturn credentials; clients refresh the settings for new connections every six hours. A relay allocation lasting beyond that credential's lifetime may require reconnection. Restart the service after changing identity, crossplay or relay settings. Name, news and other ordinary settings retain the existing reload behavior.

Back up the complete `local/data/` directory, especially the TLS private key: player accounts and certificate pins are scoped to that key. Replacing it creates a new community identity domain. Clients retain their encrypted account separately from the removable server list. Deleting client account files creates a new account; these are guest identities, not proof of a unique human. Existing address bans and connection limits remain available.

Optional Steam integration requires Steam on the player's PC. Add a Steam Web API key to `local/server.toml` to verify optional Steam identities and enable cross-platform Recently Played With reporting. With no key, native accounts, room codes and Steam invitations still work, but Steam bindings remain zero and cannot be reported as verified opponents. Keep `trust_unverified_steam_ids = false` for public hosting; it applies only to legacy login.

The relay configuration sets per-user allocation and bandwidth limits, a total allocation limit, and a 64 MiB/s bandwidth cap. Adjust these to the host's capacity. It denies loopback, link-local and private peer destinations to prevent authenticated clients from using it to access private services. LAN-only hosting may need deliberate changes to those address restrictions. It does not require `allow-loopback-peers`.

To estimate relay costs, measure coturn's interface bytes during representative fights with spectators. A relayed packet enters and leaves the relay, so interface traffic is roughly twice the relayed gameplay payload plus protocol overhead. Direct sessions use the service only for control traffic. The included smoke test proves routing and delivery; its synthetic traffic is not a game bandwidth estimate.

Build the game with `TTT2_CROSSPLAY=ON` using [the networking build instructions](../../../port/tools/crossplay/README.md). Players choose **ONLINE MODE > SERVER > STEAM** for Steam-only rooms, or select this server before creating a cross-platform room. Leave the current room before changing networks. A lost community connection ends cross-platform connectivity and never converts that room into a Steam-only room.
