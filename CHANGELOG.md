# 更新日志

## 1.0.4

首个公开发布版本，包含本地迭代完成的功能：

- 基于 Saleae SPI 低级分析器解析 TCS 和 PROG 协议。
- 校验请求头、DATA 和返回 CRC，关联 TCS 请求/应答，支持同 CS 轮询。
- 自动识别 MOSI/MISO 请求方向，也支持手动指定。
- 识别进入 PROG、数字/模拟/扩展 Die 切换，支持 `0x83`。
- Type 统一为 TCS Request、TCS Replay、PROG、Special。
- 使用 CMDID 列显示 TCS 命令或 PROG 地址，事务编号固定十进制。
- 精简气泡和表格显示，PROG 使用 `PROG read/write 0x地址 N bytes = DATA`。
- 提供中文使用说明、协议说明、测试摘要、离线回放与打包脚本。
- 采用 MIT 许可证。
