package main

import (
	"net"
	"net/url"
)

// benchmarkNet (198.18.0.0/15) is what proxy TUN modes (Clash, Surge...) put on their virtual interface.
var benchmarkNet = &net.IPNet{IP: net.IPv4(198, 18, 0, 0), Mask: net.CIDRMask(15, 32)}

// localIPs lists this computer's IPv4 addresses for the hub's host list: first the
// one used to reach the hub (a UDP "dial" only picks a route, nothing is sent),
// then the other non-loopback interfaces.
func localIPs(hubURL string) []string {
	var ips []string
	seen := map[string]bool{}
	add := func(ip net.IP) {
		if v4 := ip.To4(); v4 != nil && !v4.IsLoopback() && !v4.IsLinkLocalUnicast() && !benchmarkNet.Contains(v4) && !seen[v4.String()] {
			seen[v4.String()] = true
			ips = append(ips, v4.String())
		}
	}
	if u, err := url.Parse(hubURL); err == nil && u.Hostname() != "" {
		port := u.Port()
		if port == "" {
			port = "80"
		}
		if conn, err := net.Dial("udp", net.JoinHostPort(u.Hostname(), port)); err == nil {
			if addr, ok := conn.LocalAddr().(*net.UDPAddr); ok {
				add(addr.IP)
			}
			conn.Close()
		}
	}
	if addrs, err := net.InterfaceAddrs(); err == nil {
		for _, a := range addrs {
			if n, ok := a.(*net.IPNet); ok && len(ips) < 8 {
				add(n.IP)
			}
		}
	}
	return ips
}
