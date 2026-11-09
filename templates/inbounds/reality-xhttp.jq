# VLESS + Reality + XHTTP，外加 VLESS Encryption（ML-KEM-768）
{
  remark: "\($node)-reality-xhttp",
  enable: true,
  expiryTime: 0,
  total: 0,
  listen: "",
  port: ($xhttp_port | tonumber),
  protocol: "vless",
  tag: "in-\($xhttp_port)-tcp",
  settings: {
    clients: [$client + {id: $uuid, flow: "xtls-rprx-vision"}],
    decryption: $vless_dec,
    encryption: $vless_enc,
    fallbacks: []
  },
  streamSettings: {
    network: "xhttp",
    xhttpSettings: {
      path: $xhttp_path,
      host: "",
      mode: "auto",
      scMaxBufferedPosts: 30,
      scStreamUpServerSecs: "20-80",
      xPaddingBytes: "100-1000"
    },
    security: "reality",
    realitySettings: {
      show: false,
      xver: 0,
      target: $reality_target,
      serverNames: [$reality_domain],
      privateKey: $xhttp_priv,
      minClientVer: "",
      maxClientVer: "",
      maxTimediff: 0,
      shortIds: ($xhttp_sids | split(",")),
      mldsa65Seed: "",
      settings: {
        publicKey: $xhttp_pub,
        fingerprint: "chrome",
        serverName: "",
        spiderX: "/",
        mldsa65Verify: ""
      }
    }
  },
  sniffing: $sniffing
}
