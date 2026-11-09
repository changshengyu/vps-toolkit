# Xray 模板中由脚本管理的部分：出站和路由规则
# 第二阶段会把它合并进面板当前的模板，其他字段（log、api、policy 等）保持不变。
{
  outbounds: (
    [
      {
        tag: "direct",
        protocol: "freedom",
        settings: {
          finalRules: [
            {action: "block", ip: ["geoip:private"]},
            {action: "allow"}
          ]
        }
      },
      {tag: "blocked", protocol: "blackhole", settings: {}}
    ]
    + (if $ai == "y" then [{
        tag: "六六云-ISP",
        protocol: "shadowsocks",
        settings: {
          servers: [{
            address: $ss_addr,
            port: ($ss_port | tonumber),
            method: $ss_method,
            password: $ss_pass,
            uot: false,
            UoTVersion: 1
          }]
        },
        streamSettings: {
          network: "tcp",
          security: "none",
          tcpSettings: {header: {type: "none"}}
        }
      }] else [] end)
  ),
  routing: {
    domainStrategy: "AsIs",
    rules: (
      [{type: "field", inboundTag: ["api"], outboundTag: "api"}]
      + (if $ai == "y" then [
          # AI 网站的 QUIC 屏蔽掉，迫使客户端回落到 TCP
          {type: "field", domain: ["geosite:category-ai-!cn"], network: "udp", port: "443", outboundTag: "blocked"},
          {type: "field", domain: ["geosite:category-ai-!cn"], network: "tcp", outboundTag: "六六云-ISP"}
        ] else [] end)
      + [
          # 同一条规则里的 domain 和 ip 需要同时满足，所以拆成两条
          {type: "field", domain: ["geosite:cn"], outboundTag: "blocked"},
          {type: "field", ip: ["geoip:cn", "geoip:private"], outboundTag: "blocked"},
          {type: "field", protocol: ["bittorrent"], outboundTag: "blocked"}
        ]
    )
  }
}
