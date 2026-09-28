# 协议说明

本实现依据 ICNT9268S 的参考协议资料和采样数据。字段布局固定，不保证兼容其他芯片或固件变体。

## TCS

```text
读请求: F1 CMD_L CMD_H LEN_L LEN_H HCRC_L HCRC_H
写请求: F0 CMD_L CMD_H LEN_L LEN_H HCRC_L HCRC_H DATA[N] DCRC_L DCRC_H
读应答: DATA[N] ErrCode CMD_L CMD_H CRC_L CRC_H
写应答: ErrCode CMD_L CMD_H CRC_L CRC_H
```

CMD 和 LEN 均为小端。CMD_H 的 bit6 表示读，bit5 表示写，低 5 位为逻辑类号。普通应答发生在下一次 CS 事务，主机发送全 FF 占位；`F1 06 51` 轮询在同一次 CS 内从返回通道第 0 字节接收完整数据帧。

CRC：poly=`0x8005`，init=`0`，xorout=`0`，不反射、MSB 递推，线上小端。

- 请求头 CRC 覆盖字节 0–4。
- 写数据 CRC 只覆盖 DATA。
- 返回 CRC 覆盖 DATA（如有）、ErrCode 和回显 CMD。

只有关联请求、长度、CRC、ErrCode 和回显均有效，内部结果才视为有效。全零返回不会因为 CRC 数学上等于零而被当作成功 ACK。

## PROG

```text
OP ADDR[3] LEN[3] HCRC[2] DUMMY[4] DATA[N] DCRC[2] TAIL[3]
```

OP=`60` 写、`61` 读；ADDR、LEN 和 CRC 均为大端。数据从字节偏移 13 开始：写操作取主机通道，读操作取芯片返回通道。事务总长度为 N+18。

CRC 核心与 TCS 相同，但 xorout=`0xFFFF`。头 CRC 覆盖字节 0–6，数据 CRC 只覆盖 DATA。

尾部最后两字节为 `0A 0A` 时识别为 ACK；任一字节为 `E0` 时报告错误；其他尾部值标为未识别应答。不会自动移位或修补异常数据。

## 特殊序列

| 完整主机事务 | 操作 |
|---|---|
| `CC 33 55 5A` | 请求进入 PROG |
| `81` | 切换数字 Die |
| `82` | 切换模拟 Die |
| `83` | 切换扩展 Die |

仅精确匹配完整 CS 事务；例如 `83 00` 不会被误认成 Die 切换。`0x83` 来自扩展需求，未假定具体芯片型号。

出现 `61` 不能单独证明芯片已进入 PROG。进入序列只代表发送请求；有效读取 `0x070010`、返回第二字节低 3 位为 2 时，内部模式才标记为已确认 PROG。

## 参考接口

- [Saleae SPI 输入帧定义](https://docs.saleae.com/extensions/guides/analyzer_framev2#serial-peripheral-interface-spi)
- [Saleae 高级分析器开发说明](https://www.saleae.com/support/extensions-api/extensions/high-level-analyzer-extensions)

芯片协议由本地参考资料和采样交叉验证，原始采样及厂商源码不随仓库分发。
