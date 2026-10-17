# CSV 模板、预检与确认入库

已提供预检、确认入库和保留期清理。上传及预检不写业务表；上传者确认后由 Worker 整批执行，只有 SUCCEEDED 才表示导入完成。2026-10-17 已在隔离 MySQL 8.4 完成完整回归；云数据库恢复后的环境复验单独进行。

## 使用步骤

1. 登录后进入“CSV 批量导入”，选择类型。除店铺模板外，必须选择一个正常店铺。
2. 下载并填写模板，保留全部英文列名；可调整列顺序，不能增加、遗漏或重复表头。可选字段的单元格可以留空。
3. 保存为 UTF-8 CSV（兼容 BOM）；文件最多 10 MB、10,000 行数据，不支持 Excel 文件。
4. 上传后生成持久批次，由 Worker 预检。页面可见时每 5 秒刷新处理中的批次，页面失焦暂停、恢复可见立即刷新；刷新页面不会丢失结果。若一直等待，请检查 Worker 是否运行。
5. 查看新增、覆盖、错误行数量及逐行差异。有错误时整批不可入库；可下载错误清单，修正后重新上传。同文件再次上传会先提示重复，明确确认后才创建新批次。
6. 上传者在预检有效期内确认入库；存在覆盖行时必须先勾选允许覆盖。确认后显示“入库中”，等待“入库成功”或失败说明；遇版本变化或过期，重新上传预检。

## 模板字段

| 类型值 | 内容 | 必填字段 | 可空字段 |
| --- | --- | --- | --- |
| stores | 店铺，仅管理员 | code、name、platform | external_id、category、notes |
| products | 商品与 SKU | code、name、category、selling_points、sku_code、spec_name、price | brand、audience、source_url、cost |
| competitors | 竞品 | code、name、platform、source_note、collected_at | brand、spec、price、selling_points、excerpt、source_url、product_code |
| store | 店铺日经营 | business_date、paid_amount、paid_orders、visitors、refund_amount | 无 |
| product | 商品日经营 | product_code、business_date、paid_amount、paid_orders、visitors、refund_amount | 无 |
| ad | 投放日经营 | channel、business_date、spend、impressions、clicks | attributed_amount、attribution_note；提供归因金额时口径必填 |

商品模板一行一个 SKU。同商品各行的商品级字段必须一致，SKU 编码全店唯一，不能关联到其他商品。文件内业务键重复时，所有重复行均报错，不静默去重。

入库时同商品合并更新一次，文件中未出现的已有 SKU 保留，已有 SKU 使用稳定 ID，不删除或改编码；合并后每商品最多 100 个 SKU。三类日经营记录的 batch_id 保存最近一次 CSV 写入批次，手工创建可为空；后续手工编辑仍保留该历史来源，当前值的修改过程以审计为准。

product_code 填所选店铺内的商品编码，不是数据库 ID，不跨店匹配。商品日经营只能关联正常商品；竞品可不关联本品，原有关联归档商品时可保留。归档店铺及目标资料不能通过导入绕过只读限制。

经营日期采用 YYYY-MM-DD 和 Asia/Shanghai，不接受未来日期。金额非负且最多两位小数，计数为非负整数；空白不等于零，退款可以高于当日支付。投放归因未知时留空，确认零值时填 0 并注明口径；点击大于曝光会显示警告。URL 仅作为文本保存，不抓取网页。

## 状态、权限与存储

状态：UPLOADED（等待）→ VALIDATING（处理中）→ PREVIEWED（通过）/ INVALID（行数据有误）/ FAILED（文件或处理失败）。VALIDATING 带 5 分钟租约，中断后过期任务可重新领取，迟到 Worker 不会覆盖已有结果。

确认后 PREVIEWED → IMPORTING → SUCCEEDED / FAILED。执行使用 5 分钟租约并在事务期间持有批次锁；进程中断后业务事务回滚，过期租约可被重新领取。业务记录、逐项审计及 SUCCEEDED 在同一事务提交；失败状态单独短事务记录，不留下部分业务写入。

数量按 CSV 数据行统计，商品文件一行对应一个 SKU，不应当作商品数量。管理员可查看本企业批次；主管可查看可访问店铺批次；运营人员仅查看自己上传且仍有店铺授权的批次。列表、详情、错误下载和 Worker 都检查实时权限。

原文件保存在 UPLOAD_DIR 私有目录（相对路径按 backend 解析），使用随机文件名；不公开服务器路径或原文件。Worker 复核 SHA-256 后读取。错误 CSV 对公式单元格转义。预检只保存批次、规范行、错误、版本依赖和脱敏审计。

预检有效期 30 分钟，详情仅向上传者提供 preview_token 和确认入口。确认及执行均重新解析并核对文件摘要、目标/引用版本和新增业务键，变化则拒绝；执行排队超过有效期不撤销此前已及时接受的确认。只有上传者能确认，其他有权限的用户可查看结果。

首版按企业串行执行确认/入库：锁企业→账号→授权→店铺→批次→相关业务记录，复核使用 MySQL 当前读；并发吞吐与万行入库耗时尚未压测。入库量大时业务事务可能较长。

## 接口及后续验证

- GET `/api/v1/import-templates/{type}`：模板下载。
- POST `/api/v1/imports`：multipart 字段 type、store_id（店铺模板不传）、file；明确再次上传可加 allow_duplicate=true；返回 202。
- GET `/api/v1/imports?page=1`：批次列表，每页 20 条。
- GET `/api/v1/imports/{id}?page=1`：预检及逐行差异，每页 20 行。
- GET `/api/v1/imports/{id}/errors`：错误 CSV。
- POST `/api/v1/imports/{id}/confirm`：请求头 Idempotency-Key（1–64 位字母、数字、下划线、连字符）；JSON 为 `{ "preview_token": "详情返回的令牌", "allow_overwrite": false }`，返回 202。重试同批次必须保持键和请求体一致，返回当前状态；换键、换请求体或将同一键用于另一批次返回 409。

新增迁移 `0009_import_confirm` 为三类日经营增加 batch_id，`0010_import_retention` 增加完成及清理标记。隔离 MySQL 8.4 已升级到 head，`alembic check` 和 160 项完整回归通过；云数据库恢复后仍须在目标环境执行迁移和连接复验。

## 自动清理

Worker 就绪后首次执行清理，默认每小时扫描一次；可设置 IMPORT_CLEANUP_INTERVAL_SECONDS（60–86400 秒）。每轮每类最多处理 20 个批次，逐批事务并跳过其他 Worker 已锁定的项。

- PREVIEWED 超时后转 FAILED，保留 IMPORT_PREVIEW_EXPIRED 提示，要求重新上传；不会修改已确认的 IMPORTING。
- SUCCEEDED / FAILED / INVALID 完成满 7 天后删除规范明细及版本依赖，保留批次行数、错误计数、状态及摘要。接口返回 rows_purged_at；错误下载返回 410 IMPORT_DETAILS_EXPIRED，页面显示保留期提示。
- 原 CSV 上传满 30 天且批次已终态时删除，返回 file_deleted_at。排队、预检或入库中的批次不会被删除文件。
- 删除仅限系统生成文件名对应的私有目录 CSV，拒绝路径穿越和文件符号链接；文件已不存在可幂等补记标记。文件删除失败保存脱敏审计 IMPORT_CLEANUP_FAILED，延迟 24 小时重试，其他批次仍可继续处理。
- 批次摘要、审计和所有业务事实不自动删除，满足至少 180 天保留要求；清理操作自身同样记录审计。数据库故障写入 Worker 的 import_cleanup_failed 日志事件，需由部署方采集监控，不宣称已接入外部告警平台。

完成时间使用独立 finished_at，不受后续清理更新 updated_at 影响；新增清理时间字段为 DATETIME(6)，避免重试边界被秒级舍入改变。文件与数据库不共享事务：先删终态文件，再提交标记，进程中断后允许补记。

AI 任务执行属于 M2，尚未开放。
