# VLESS + Reality + Vision（TCP）
{
  remark: "\($node)-reality-vision",
  enable: true,
  expiryTime: 0,
  total: 0,
  listen: "",
  port: ($reality_port | tonumber),
  protocol: "vless",
  tag: "in-\($reality_port)-tcp",
  settings: {
    clients: [$client + {id: $uuid, flow: "xtls-rprx-vision"}],
    decryption: "none",
    encryption: "none",
    testseed: [900, 500, 900, 256]
  },
  streamSettings: {
    network: "tcp",
    tcpSettings: {acceptProxyProtocol: false, header: {type: "none"}},
    security: "reality",
    realitySettings: {
      show: false,
      xver: 0,
      target: $reality_target,
      serverNames: [$reality_domain],
      privateKey: $tcp_priv,
      minClientVer: "",
      maxClientVer: "",
      maxTimediff: 0,
      shortIds: ($tcp_sids | split(",")),
      mldsa65Seed: "",
      settings: {
        publicKey: $tcp_pub,
        fingerprint: "chrome",
        serverName: "",
        spiderX: "/",
        mldsa65Verify: ""
      }
    }
  },
  sniffing: $sniffing,
  shareAddrStrategy: "custom",
  shareAddr: $hy2_domain
}
