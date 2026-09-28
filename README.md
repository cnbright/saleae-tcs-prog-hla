# TCS & PROG SPI 高级分析器

用于 Saleae Logic 2 的 SPI 高级分析器，解析 ICNT9268S 的 TCS、PROG 协议及进入编程模式、Die 切换等特殊序列。

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)

## 显示效果

```text
TCS Request CMD=5003 read 4 bytes
TCS Replay data = DB 92 00 00
TCS Request CMD=3109 write 1 bytes data = 00
TCS Replay ACK
PROG read 0x070010 4 bytes = 02 02 00 00
PROG write 0x070008 1 bytes = FE
Switch to extended DIE (0x83)
```

波形文字只保留读写、命令或地址、字节数和数据。CRC 在后台校验，不显示单独的 CRC、LEN 或命令说明；异常事务以 `[error]` 或 `[warning]` 提示。长数据气泡预览前 32 字节，表格中的 `payload` 保留完整数据。

## 安装

1. 从 [Releases](https://github.com/cnbright/saleae-tcs-prog-hla/releases) 下载最新版安装包并解压；也可以克隆本仓库。
2. 在 Logic 2 的 **Extensions** 面板选择 **Load Existing Extension**，打开解压目录中的 `extension.json`。
3. 为采样添加内置 **SPI** 低级分析器，配置好 SCK、CS、MOSI、MISO。
4. 添加 **TCS & PROG** 高级分析器，Input Analyzer 选择上一步的 SPI。
5. 更新源码后点击 **Reload Source Files**。若旧列仍保留，可移除并重新添加该高级分析器。

参考采样使用以下 SPI 设置：

| 配置 | 值 |
|---|---|
| 每次传输位数 | 8 bits |
| 位序 | MSB first |
| 时钟模式 | Mode 0，CPOL=0 / CPHA=0 |
| 片选 | CS 低有效，必须配置 Enable 通道 |

不需要安装 Python 依赖；插件使用 Logic 2 自带的 Python 环境。离线测试、回放和打包使用 Python 3.10 或更新版本。

## 分析器设置

| 设置 | 选项 | 说明 |
|---|---|---|
| Host request line | Auto / MOSI / MISO | Auto 根据有效请求头 CRC 或完整特殊序列锁定主机请求方向；接线反置时可指定 MISO |
| Display | Transactions / Fields | 每个 CS 显示一条摘要，或按字段时间显示 |

Auto 锁定后不会随返回数据切换方向。从应答中间开始采样、两条线同时出现候选请求时，请手动选择方向。同 CS 的 TCS 轮询存在双向数据重叠，即使选择 Fields，也会使用一条完整事务摘要。

## 表格列

| 列 | 内容 |
|---|---|
| Type | TCS Request、TCS Replay、PROG、Special |
| transaction | 十进制事务编号，从 1 开始 |
| CMDID | 合并后的 TCS 命令 ID（如 `0x5003`）或完整 PROG 地址（如 `0x070010`） |
| rw | READ / WRITE |
| payload | 有效 DATA，十六进制字节 |
| request_transaction | TCS 应答关联的请求编号，十进制 |
| echo_command | 芯片实际回显的合并 TCS 命令 ID |
| special | 进入 PROG、Die 切换等特殊操作 |
| summary | 与波形一致的简洁说明 |

Fields 模式还会提供 `field` / `value`。Type 中的 TCS Request 包括同 CS 轮询，其摘要同时包含返回数据。未知 SPI 序列和输入错误归入 Special 并明确标注。

## 支持的特殊操作

| 完整 SPI 主机序列 | 含义 |
|---|---|
| `CC 33 55 5A` | 请求进入 PROG 模式 |
| `81` | 切换数字 Die |
| `82` | 切换模拟 Die |
| `83` | 切换扩展 Die |

序列必须占据完整 CS 事务，不做前缀匹配。进入 PROG 的请求不等于状态已确认；内部会结合有效 Boot 状态读取判断，单独出现 PROG 读命令不会被当作已经进入编程模式。

## 校验与边界

- TCS：请求头 CRC、写数据 CRC、应答 CRC、长度、读写标志、ErrCode 与命令回显。
- PROG：请求头 CRC、数据 CRC、长度与尾部 ACK。
- 支持普通分离式 TCS 请求/应答和 `F1 06 51` 同 CS 轮询。
- 原始数据不修补：错误 CRC、全零 ACK、丢失应答会保留为异常。
- 需要完整 CS 边界；片选开始前的字节被忽略，缺少结束边界的最后一笔不输出。
- 每个 CS 最多缓冲 1 MiB，超过上限明确报错；不接受 16/32 位 SPI 传输设置。
- 面向参考芯片协议，其他芯片或固件变体请先核对 [协议说明](docs/PROTOCOL.md)。

## 开发与测试

```sh
python -m unittest discover -v
python tools/build_release.py
```

打包输出在 `dist/`，包括可直接加载的扩展 ZIP 和 `SHA256SUMS.txt`。打包使用固定文件清单，不会包含原始采样、本地回放报告、缓存或测试辅助模块。

离线解析 Saleae SPI Data Table 导出的十六进制 CSV：

```sh
python replay.py capture.csv --direction Auto --output decoded.json
```

CSV 需要包含 `type`、`start_time`、`duration`、`mosi`、`miso` 列。也支持逐事务 JSON，包含 `start_s`、`end_s`、`mosi`、`miso` 字段。`.kvdat` 不属于该命令的直接输入格式。

如已持有原始参考数据目录，可执行可选的完整回归：

```sh
python -m tests.validate_reference --reference /path/to/reference
```

原始参考数据不随仓库分发。当前测试覆盖 21 项单元测试、56 项参考断言和 2,610 笔参考事务，详见 [测试摘要](docs/TEST_REPORT.md)。回归通过表示解析与预期一致，不表示所有输入通信数据都有效。

## 项目结构

```text
extension.json           Saleae 扩展清单
HighLevelAnalyzer.py     Saleae SPI 帧适配与界面输出
protocol.py              独立协议解析与校验
command_names.py        离线诊断用 TCS 命令名称
replay.py               离线 CSV / JSON 回放工具
tests/                  单元测试、适配测试与可选参考回归
tools/build_release.py  可重复构建的安装包脚本
docs/                   协议说明、测试摘要、发布说明
```

## 常见问题

**波形有解析，但右侧表格为空？** 清空 Data 搜索框中的过滤文本。

**请求和返回方向不对？** 将 Host request line 从 Auto 改为实际承载主机请求的 MOSI 或 MISO，然后重新加载。

**有 ERROR 或 WARNING？** 输入可能有 CRC、长度、回显或 ACK 异常。可用离线回放工具查看完整诊断，不应仅凭 ErrCode=0 判定应答有效。

## 许可证

本项目采用 [MIT License](LICENSE)。原始采样、厂商源码及第三方资料不包含在许可证授权范围内，也不随项目分发。
