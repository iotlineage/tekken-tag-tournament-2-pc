package hub

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha1"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"time"
	"ttt2-server/internal/wire"
)

// Accounts are scoped to the pinned server key. Credentials are bearer secrets,
// transmitted only over TLS, never in logs or lobby metadata. Back up the TLS key.
func (h *Hub) communityIdentity(token string) (uint64, string, error) {
	key, err := x509.MarshalPKCS8PrivateKey(h.id.Cert.PrivateKey)
	if err != nil {
		return 0, "", err
	}
	mac := func(b []byte) []byte {
		m := hmac.New(sha256.New, key)
		m.Write([]byte("ttt2-community-account-v1\x00"))
		m.Write(b)
		return m.Sum(nil)
	}
	if token == "" {
		b := make([]byte, 32)
		if _, err := rand.Read(b); err != nil {
			return 0, "", err
		}
		// Distinct from Steam and LOOP IDs; fits SQLite's signed int64.
		id := binary.LittleEndian.Uint64(b)&0x0000ffffffffffff | 0x4350000000000000
		binary.LittleEndian.PutUint64(b, id)
		token = base64.RawURLEncoding.EncodeToString(append(b, mac(b)...))
		return id, token, nil
	}
	b, err := base64.RawURLEncoding.DecodeString(token)
	if err != nil || len(b) != 64 || !hmac.Equal(b[32:], mac(b[:32])) {
		return 0, "", errors.New("invalid account credential")
	}
	id := binary.LittleEndian.Uint64(b)
	if id>>48 != 0x4350 {
		return 0, "", errors.New("invalid account namespace")
	}
	return id, token, nil
}

// coturn REST shared-secret credentials. The secret itself never leaves the server.
func turnCredential(secret string, peer uint64, now time.Time) (string, string) {
	if secret == "" {
		return "", ""
	}
	user := fmt.Sprintf("%d:%016x", now.Add(24*time.Hour).Unix(), peer)
	m := hmac.New(sha1.New, []byte(secret))
	m.Write([]byte(user))
	return user, base64.StdEncoding.EncodeToString(m.Sum(nil))
}

// Browsing the original title probes the host before ROOM_JOIN. These leases
// allow signaling only; clients restrict non-member traffic to UDP discovery.
// They neither consume a room slot nor confer a Steam player-history identity.
type discoveryLease struct {
	room  uint64
	until time.Time
}

const discoverySeconds = 30
const maxDiscoveryPeers = 128

func discoveryNotice(p, q *Player, seconds uint8) {
	var w wire.Writer
	w.U64(q.Peer)
	w.U8(seconds)
	p.send(wire.DiscoveryPeer, w.B)
}
func (h *Hub) pruneDiscoveryLocked(p *Player) {
	now := time.Now()
	for q, lease := range p.discovery {
		s := h.sessions[lease.room]
		if !now.Before(lease.until) || h.players[p.Peer] != p || h.players[q.Peer] != q || s == nil || (s.host != p && s.host != q) {
			delete(p.discovery, q)
			delete(q.discovery, p)
			discoveryNotice(p, q, 0)
			discoveryNotice(q, p, 0)
		}
	}
}
func (h *Hub) grantDiscoveryLocked(p *Player, s *session) {
	q := s.host
	if !p.Crossplay || !q.Crossplay || p == q || p.Proto != q.Proto || !s.open || h.players[p.Peer] != p || h.players[q.Peer] != q {
		return
	}
	h.pruneDiscoveryLocked(p)
	h.pruneDiscoveryLocked(q)
	if _, exists := p.discovery[q]; !exists && (len(p.discovery) >= maxDiscoveryPeers || len(q.discovery) >= maxDiscoveryPeers) {
		return
	}
	if p.discovery == nil {
		p.discovery = make(map[*Player]discoveryLease)
	}
	if q.discovery == nil {
		q.discovery = make(map[*Player]discoveryLease)
	}
	lease := discoveryLease{s.id, time.Now().Add(discoverySeconds * time.Second)}
	p.discovery[q], q.discovery[p] = lease, lease
	discoveryNotice(q, p, discoverySeconds)
	discoveryNotice(p, q, discoverySeconds)
}
func (h *Hub) canSignalLocked(p, q *Player) bool {
	h.pruneDiscoveryLocked(p)
	_, discovery := p.discovery[q]
	return discovery || h.shareRoomLocked(p, q)
}

func (h *Hub) shareRoomLocked(p, q *Player) bool {
	for _, members := range h.roomMembers {
		if members[p] && members[q] {
			return true
		}
	}
	return false
}

func (h *Hub) notifyRoomLocked(id uint64) {
	members := h.roomMembers[id]
	var w wire.Writer
	w.U64(id)
	w.U8(uint8(len(members)))
	code := ""
	if s := h.sessions[id]; s != nil {
		code = s.joinCode
	}
	w.Str(code)
	for p := range members {
		w.U64(p.Peer)
		w.U64(p.SteamID)
	}
	for p := range members {
		p.send(wire.RoomPeers, w.B)
	}
}

func (h *Hub) removeRoomLocked(id uint64) {
	for _, player := range h.players {
		for q, lease := range player.discovery {
			if lease.room == id {
				delete(player.discovery, q)
				delete(q.discovery, player)
				discoveryNotice(player, q, 0)
				discoveryNotice(q, player, 0)
			}
		}
	}
	var w wire.Writer
	w.U64(id)
	w.U8(0)
	w.Str("")
	for p := range h.roomMembers[id] {
		p.send(wire.RoomPeers, w.B)
	}
	delete(h.roomMembers, id)
}

func (h *Hub) crossplayFrame(p *Player, typ uint8, payload []byte) error {
	if !p.Crossplay {
		return errors.New("crossplay request on a legacy connection")
	}
	r := wire.Reader{B: payload}
	now := time.Now()
	switch typ {
	case wire.RelayRequest:
		if len(payload) != 0 {
			return errors.New("bad RELAY_REQUEST")
		}
		if !p.rooms.take(4, 8, now) {
			return nil
		}
		cfg := h.Config()
		user, password := turnCredential(cfg.TURNSecret, p.Peer, now)
		var w wire.Writer
		w.Str(user)
		w.Str(password)
		p.send(wire.RelayCredentials, w.B)
	case wire.RoomJoin:
		request, id := r.U32(), r.U64()
		code := r.Str()
		if r.Err() != nil || len(r.B) != 0 {
			return errors.New("bad ROOM_JOIN")
		}
		ok := false
		if p.rooms.take(4, 8, now) {
			h.mu.Lock()
			s := h.sessions[id]
			if h.players[p.Peer] == p && s != nil && s.host.Crossplay && s.host.Proto == p.Proto {
				members := h.roomMembers[id]
				joined := 0
				for _, group := range h.roomMembers {
					if group[p] {
						joined++
					}
				}
				if members[p] || ((s.open || (code != "" && constantTimeEqual(code, s.joinCode))) && len(members) < int(s.max) && joined < maxSessionsEach) {
					members[p] = true
					ok = true
					h.notifyRoomLocked(id)
				}
			}
			h.mu.Unlock()
		}
		var w wire.Writer
		w.U32(request)
		if ok {
			w.U8(1)
		} else {
			w.U8(0)
		}
		p.send(wire.RoomResult, w.B)
	case wire.RoomLeave:
		id := r.U64()
		if r.Err() != nil || len(r.B) != 0 {
			return errors.New("bad ROOM_LEAVE")
		}
		h.mu.Lock()
		if members := h.roomMembers[id]; members != nil {
			delete(members, p)
			h.notifyRoomLocked(id)
		}
		h.mu.Unlock()
	case wire.Signal:
		to, data := r.U64(), r.Blob()
		if r.Err() != nil || len(r.B) != 0 || len(data) > 16384 || len(data) == 0 {
			return errors.New("bad SIGNAL")
		}
		if !p.signals.take(20, 40, now) {
			return nil
		}
		h.mu.Lock()
		q := h.players[to]
		if h.players[p.Peer] == p && q != nil && q != p && q.Crossplay && q.Proto == p.Proto && h.canSignalLocked(p, q) && !q.failing.Load() {
			var w wire.Writer
			w.U64(p.Peer)
			w.Blob(data)
			q.send(wire.SignalRecv, w.B)
		}
		h.mu.Unlock()
	}
	return nil
}
