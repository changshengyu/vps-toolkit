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
    # 入站开启了 routeOnly 嗅探，路由看到的多是域名；IPIfNonMatch 让 IP 规则也能作用于域名请求
    domainStrategy: "IPIfNonMatch",
    rules: (
      [{type: "field", inboundTag: ["api"], outboundTag: "api"}]
      + (if $ai == "y" then [
          # AI 网站的 QUIC 屏蔽掉，迫使客户端回落到 TCP
          {type: "field", domain: ["geosite:category-ai-!cn"], network: "udp", port: "443", outboundTag: "blocked"},
          {type: "field", domain: ["geosite:category-ai-!cn"], network: "tcp", outboundTag: "六六云-ISP"}
        ] else [] end)
      + [
          # 屏蔽回国流量：按 IP 判断（Xray 官方服务端教程的做法）。
          # 配合 domainStrategy IPIfNonMatch，未命中规则的域名会被解析成 IP 再匹配，
          # 所以解析到国内 IP 的网站也会被屏蔽。
          # 不要按域名屏蔽 geosite:cn：该列表包含 www.gstatic.com 等域名，
          # 会屏蔽 mihomo 的默认测速地址，导致节点全部显示不可用。
          {type: "field", ip: ["geoip:private", "geoip:cn"], outboundTag: "blocked"},
          {type: "field", protocol: ["bittorrent"], outboundTag: "blocked"}
        ]
    )
  }
}
