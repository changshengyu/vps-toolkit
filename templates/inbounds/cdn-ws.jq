# VLESS + WS + TLS，经 Cloudflare CDN
{
  remark: "\($node)-cdn-ws",
  enable: true,
  expiryTime: 0,
  total: 0,
  listen: "",
  port: ($cdn_port | tonumber),
  protocol: "vless",
  tag: "in-\($cdn_port)-tcp",
  settings: {
    clients: [$client + {id: $uuid, flow: ""}],
    decryption: "none",
    encryption: "none",
    fallbacks: []
  },
  streamSettings: {
    network: "ws",
    wsSettings: {
      acceptProxyProtocol: false,
      path: $ws_path,
      host: $cdn_domain,
      headers: {},
      heartbeatPeriod: 0
    },
    security: "tls",
    tlsSettings: {
      serverName: $cdn_domain,
      alpn: ["http/1.1"],
      minVersion: "1.2",
      maxVersion: "1.3",
      cipherSuites: "",
      rejectUnknownSni: true,
      disableSystemRoot: false,
      enableSessionResumption: true,
      echServerKeys: "",
      certificates: [{
        certificateFile: "/root/cert/\($cdn_domain)/fullchain.pem",
        keyFile: "/root/cert/\($cdn_domain)/privkey.pem",
        ocspStapling: 0,
        oneTimeLoading: false,
        usage: "encipherment",
        buildChain: false
      }],
      settings: {fingerprint: "chrome", echConfigList: ""}
    }
  },
  sniffing: $sniffing
}
