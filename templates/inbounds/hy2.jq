# Hysteria2 入站：TLS 证书 + salamander 混淆 + 端口跳跃
{
  remark: "\($node)-hy2",
  enable: true,
  expiryTime: 0,
  total: 0,
  listen: "",
  port: ($hy2_port | tonumber),
  protocol: "hysteria",
  tag: "in-\($hy2_port)-udp",
  settings: {
    version: 2,
    clients: [$client + {auth: $hy2_auth}]
  },
  streamSettings: {
    network: "hysteria",
    security: "tls",
    tlsSettings: {
      serverName: $hy2_domain,
      alpn: ["h3"],
      minVersion: "1.2",
      maxVersion: "1.3",
      cipherSuites: "",
      rejectUnknownSni: false,
      disableSystemRoot: false,
      enableSessionResumption: false,
      echServerKeys: "",
      certificates: [{
        certificateFile: "/root/cert/\($hy2_domain)/fullchain.pem",
        keyFile: "/root/cert/\($hy2_domain)/privkey.pem",
        ocspStapling: 0,
        oneTimeLoading: false,
        usage: "encipherment",
        buildChain: false
      }],
      settings: {fingerprint: "chrome", echConfigList: ""}
    },
    hysteriaSettings: {
      version: 2,
      udpIdleTimeout: 60,
      masquerade: {
        type: "", dir: "", url: "", rewriteHost: false, insecure: false,
        content: "", headers: {}, statusCode: 0
      }
    },
    finalmask: {
      quicParams: {
        congestion: "brutal",
        brutalUp: "0 mbps",
        brutalDown: "0 mbps",
        debug: false,
        disablePathMTUDiscovery: false,
        initStreamReceiveWindow: 8388608,
        maxStreamReceiveWindow: 8388608,
        initConnectionReceiveWindow: 20971520,
        maxConnectionReceiveWindow: 20971520,
        maxIdleTimeout: 30,
        keepAlivePeriod: 10,
        maxIncomingStreams: 1024,
        udpHop: {ports: $hop_range, interval: "30"}
      },
      udp: [{type: "salamander", settings: {password: $salamander}}]
    }
  },
  sniffing: $sniffing
}
