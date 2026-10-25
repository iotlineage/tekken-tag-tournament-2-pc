package hub

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"strings"
	"testing"
	"time"
	"ttt2-server/internal/config"
	"ttt2-server/internal/wire"
)

func crossHello(w *wire.Writer) { w.U16(3); w.Str(proto); w.Str("crossplay-test") }
func crossAuth(token string) func(*wire.Writer) {
	return func(w *wire.Writer) {
		w.U64(0)
		w.Str("PLAYER")
		w.Blob([]byte(token))
		w.Str("")
		w.U64(0)
		w.Blob(nil)
	}
}
func crossLogin(t *testing.T, s *server, token string) (*client, string) {
	t.Helper()
	c, typ, r := s.handshake(t, crossHello, crossAuth(token))
	if typ != wire.Welcome {
		t.Fatalf("crossplay WELCOME: %d", typ)
	}
	peer := r.U64()
	r.U8()
	r.Str()
	r.Str()
	r.Str()
	issued := r.Str()
	if peer>>48 != 0x4350 || issued == "" || r.U64() != 0 {
		t.Fatal("identity or unverified Steam binding")
	}
	r.Str()
	r.Str()
	r.Str()
	r.Str()
	if r.Err() != nil || len(r.B) != 0 {
		t.Fatal("welcome format")
	}
	return &client{t: t, s: s, c: c, peer: peer}, issued
}
func TestCommunityCredentials(t *testing.T) {
	cfg := config.Default()
	cfg.Crossplay = true
	s := start(t, cfg, nil)
	a, token := crossLogin(t, s, "")
	b, _ := crossLogin(t, s, token)
	if a.peer != b.peer {
		t.Fatal("reconnect changed identity")
	}
	if !closedSoon(a.c, time.Second*3) {
		t.Fatal("old account connection remained")
	}
	bad := []byte(token)
	if bad[0] == 'A' {
		bad[0] = 'B'
	} else {
		bad[0] = 'A'
	}
	_, typ, r := s.handshake(t, crossHello, crossAuth(string(bad)))
	if typ != wire.Error || r.U32() != wire.ErrAuth {
		t.Fatal("tampered token accepted")
	}
	other := start(t, cfg, nil)
	_, typ, r = other.handshake(t, crossHello, crossAuth(token))
	if typ != wire.Error || r.U32() != wire.ErrAuth {
		t.Fatal("credential crossed server identities")
	}
	_, typ, r = s.handshake(t, hello, auth(123, "spoof", nil, ""))
	if typ != wire.Error || r.U32() != wire.ErrAuth {
		t.Fatal("crossplay enabled unverified legacy identity")
	}
}
func nextType(t *testing.T, c *client, want uint8) wire.Reader {
	t.Helper()
	for i := 0; i < 8; i++ {
		typ, r := c.next()
		if typ == want {
			return r
		}
	}
	t.Fatal("missing response", want)
	return wire.Reader{}
}
func TestCrossplayRoomAuthorization(t *testing.T) {
	cfg := config.Default()
	cfg.Crossplay = true
	cfg.TrustUnverified = true
	s := start(t, cfg, nil)
	a, _ := crossLogin(t, s, "")
	b, _ := crossLogin(t, s, "")
	outsider, _ := crossLogin(t, s, "")
	legacy := s.login(t, 123, "legacy")
	a.send(wire.SessionPut, func(w *wire.Writer) { w.Blob(makeCrossRecord(88, a.peer)) })
	a.send(wire.SessionList, func(w *wire.Writer) { w.U32(7); w.U32(20) })
	nextType(t, a, wire.ListResult)
	if len(legacy.list()) != 0 {
		t.Fatal("legacy client discovered crossplay room")
	}
	b.send(wire.RoomJoin, func(w *wire.Writer) { w.U32(1); w.U64(88); w.Str("") })
	r := nextType(t, b, wire.RoomResult)
	if r.U32() != 1 || r.U8() != 1 {
		t.Fatal("room admission failed")
	}
	nextType(t, a, wire.RoomPeers)
	outsider.send(wire.Signal, func(w *wire.Writer) { w.U64(a.peer); w.Blob([]byte("unauthorized")) })
	b.send(wire.Signal, func(w *wire.Writer) { w.U64(a.peer); w.Blob([]byte("authorized")) })
	r = nextType(t, a, wire.SignalRecv)
	if r.U64() != b.peer || string(r.Blob()) != "authorized" {
		t.Fatal("signal crossed room boundary or sender spoofed")
	}
	b.send(wire.RoomLeave, func(w *wire.Writer) { w.U64(88) })
	b.send(wire.PeerCheck, func(w *wire.Writer) { w.U32(2); w.U64(a.peer) })
	r = nextType(t, b, wire.PeerResult)
	if r.U32() != 2 || r.U8() != 0 {
		t.Fatal("left peer remained authorized")
	}
	a.send(wire.SessionDel, func(w *wire.Writer) { w.U64(88) })
	a.send(wire.SessionList, func(w *wire.Writer) { w.U32(7); w.U32(20) })
	nextType(t, a, wire.ListResult)
	outsider.send(wire.RoomJoin, func(w *wire.Writer) { w.U32(3); w.U64(88); w.Str("") })
	r = nextType(t, outsider, wire.RoomResult)
	if r.U32() != 3 || r.U8() != 0 {
		t.Fatal("stale room admitted")
	}
}
func TestCrossplayDisabled(t *testing.T) {
	s := start(t, config.Default(), nil)
	_, typ, r := s.handshake(t, crossHello, crossAuth(""))
	if typ != wire.Error || r.U32() != wire.ErrProtocol {
		t.Fatal("crossplay not disabled")
	}
}

func makeCrossRecord(id, peer uint64) []byte {
	b := makeRecord(id, peer)
	b[3] = '3'
	w := wire.Writer{B: b}
	w.Str("127.0.0.1:28200")
	w.Str("")
	return w.B
}

func TestPrivateRoomCodeAndCapacity(t *testing.T) {
	cfg := config.Default()
	cfg.Crossplay = true
	s := start(t, cfg, nil)
	host, _ := crossLogin(t, s, "")
	guest, _ := crossLogin(t, s, "")
	extra, _ := crossLogin(t, s, "")
	rec := makeCrossRecord(99, host.peer)
	// A private two-person room: no public slots, one occupied private slot.
	binary.LittleEndian.PutUint32(rec[32:], 0)
	binary.LittleEndian.PutUint32(rec[36:], 2)
	binary.LittleEndian.PutUint32(rec[40:], 0)
	binary.LittleEndian.PutUint32(rec[44:], 1)
	host.send(wire.SessionPut, func(w *wire.Writer) { w.Blob(rec) })
	roster := nextType(t, host, wire.RoomPeers)
	if roster.U64() != 99 || roster.U8() != 1 {
		t.Fatal("host roster")
	}
	code := roster.Str()
	if len(code) != 32 {
		t.Fatal("missing room capability")
	}
	join := func(c *client, code string, want bool) {
		c.send(wire.RoomJoin, func(w *wire.Writer) { w.U32(3); w.U64(99); w.Str(code) })
		r := nextType(t, c, wire.RoomResult)
		if r.U32() != 3 || (r.U8() != 0) != want {
			t.Fatal("unexpected private room admission")
		}
	}
	join(guest, "", false)
	join(guest, strings.Repeat("0", 32), false)
	join(guest, code, true)
	join(extra, code, false)
	guest.send(wire.RoomLeave, func(w *wire.Writer) { w.U64(99) })
	guest.send(wire.PeerCheck, func(w *wire.Writer) { w.U32(1); w.U64(host.peer) })
	nextType(t, guest, wire.PeerResult)
	join(extra, code, true)
	host.c.Close()
	// Disconnect removes the room and sends an empty roster to remaining peers.
	for i := 0; i < 4; i++ {
		r := nextType(t, extra, wire.RoomPeers)
		if r.U64() == 99 && r.U8() == 0 {
			return
		}
	}
	t.Fatal("host disconnect did not revoke room")
}

func TestCrossplaySteamBindings(t *testing.T) {
	cfg := config.Default()
	cfg.Crossplay = true
	const steam = 0x0110000100000042
	for _, verified := range []bool{false, true} {
		var verifier Verifier
		if verified {
			verifier = fakeVerifier{id: steam}
		}
		s := start(t, cfg, verifier)
		login := func(claim uint64) (uint8, wire.Reader) {
			_, typ, r := s.handshake(t, crossHello, func(w *wire.Writer) {
				w.U64(0)
				w.Str("PLAYER")
				w.Blob(nil)
				w.Str("")
				w.U64(claim)
				w.Blob(bytes.Repeat([]byte("ab"), 40))
			})
			return typ, r
		}
		typ, r := login(steam)
		if typ != wire.Welcome {
			t.Fatal("Steam binding login")
		}
		r.U64()
		r.U8()
		r.Str()
		r.Str()
		r.Str()
		r.Str()
		got := r.U64()
		if verified && got != steam || !verified && got != 0 {
			t.Fatal("unverified binding or missing verified binding")
		}
		if verified {
			typ, r = login(steam + 1)
			if typ != wire.Error || r.U32() != wire.ErrAuth {
				t.Fatal("spoofed Steam binding accepted")
			}
		}
	}
}

func TestRelayCredentials(t *testing.T) {
	now := time.Unix(1700000000, 0)
	user, pass := turnCredential("test-secret", 0x4350000000000042, now)
	if user != "1700086400:4350000000000042" {
		t.Fatal(user)
	}
	mac := hmac.New(sha1.New, []byte("test-secret"))
	mac.Write([]byte(user))
	if pass != base64.StdEncoding.EncodeToString(mac.Sum(nil)) {
		t.Fatal("not coturn REST credentials")
	}
}

func TestDiscoveryLeases(t *testing.T) {
	h := &Hub{players: map[uint64]*Player{}, sessions: map[uint64]*session{}, roomMembers: map[uint64]map[*Player]bool{}}
	player := func(id uint64) *Player {
		p := &Player{Peer: id, Crossplay: true, Proto: proto, out: make(chan []byte, 512)}
		h.players[id] = p
		return p
	}
	host, viewer, outsider := player(1), player(2), player(3)
	room := &session{id: 77, host: host, open: true, record: []byte("listed room")}
	h.sessions[77] = room
	h.roomMembers[77] = map[*Player]bool{host: true}
	if len(h.listSessions(viewer, 20, listFilter{})) != 1 {
		t.Fatal("room not listed")
	}
	if !h.canSignalLocked(viewer, host) || !h.canSignalLocked(host, viewer) {
		t.Fatal("discovery signaling not authorized in both directions")
	}
	if h.canSignalLocked(viewer, outsider) || h.shareRoomLocked(viewer, host) || len(h.roomMembers[77]) != 1 {
		t.Fatal("discovery conferred membership or authorized an unrelated peer")
	}
	for _, p := range []*Player{host, viewer} {
		frame := <-p.out
		if frame[4] != wire.DiscoveryPeer || len(frame) != 14 || frame[13] != discoverySeconds {
			t.Fatal("malformed discovery notice")
		}
	}
	lease := viewer.discovery[host]
	lease.until = time.Now().Add(-time.Second)
	viewer.discovery[host] = lease
	if h.canSignalLocked(viewer, host) || len(host.discovery) != 0 {
		t.Fatal("expired discovery still authorized")
	}
	h.grantDiscoveryLocked(viewer, room)
	h.removeRoomLocked(77)
	if h.canSignalLocked(viewer, host) {
		t.Fatal("deleted room still authorized")
	}
	room.open = false
	h.grantDiscoveryLocked(viewer, room)
	if h.canSignalLocked(viewer, host) {
		t.Fatal("private room exposed discovery")
	}
	room.open = true
	viewer.Proto = "other"
	h.grantDiscoveryLocked(viewer, room)
	if h.canSignalLocked(viewer, host) {
		t.Fatal("discovery crossed protocol versions")
	}
	viewer.Proto = proto
	h.grantDiscoveryLocked(viewer, room)
	delete(h.players, host.Peer)
	if h.canSignalLocked(viewer, host) {
		t.Fatal("disconnected host still authorized")
	}
}
