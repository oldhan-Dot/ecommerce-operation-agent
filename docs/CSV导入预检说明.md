# CSV 模板与预检

当前完成到预检，**不写入业务表**，确认入库将在下一步交付。预检通过不代表已经导入成功。

## 使用步骤

1. 登录后进入“CSV 导入预检”，选择类型。除店铺模板外，必须选择一个正常店铺。
2. 下载并填写模板，保留全部英文列名；可调整列顺序，不能增加、遗漏或重复表头。可选字段的单元格可以留空。
3. 保存为 UTF-8 CSV（兼容 BOM）；文件最多 10 MB、10,000 行数据，不支持 Excel 文件。
4. 上传后生成持久批次，由 Worker 预检。页面每 3 秒刷新处理中的批次，刷新页面不会丢失结果；若一直等待，请检查 Worker 是否运行。
5. 查看新增、覆盖、错误行数量及逐行差异。有错误时整批不可入库；可下载错误清单，修正后重新上传。同文件再次上传会先提示重复，明确确认后才创建新批次。

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

product_code 填所选店铺内的商品编码，不是数据库 ID，不跨店匹配。商品日经营只能关联正常商品；竞品可不关联本品，原有关联归档商品时可保留。归档店铺及目标资料不能通过导入绕过只读限制。

经营日期采用 YYYY-MM-DD 和 Asia/Shanghai，不接受未来日期。金额非负且最多两位小数，计数为非负整数；空白不等于零，退款可以高于当日支付。投放归因未知时留空，确认零值时填 0 并注明口径；点击大于曝光会显示警告。URL 仅作为文本保存，不抓取网页。

## 状态、权限与存储

状态：UPLOADED（等待）→ VALIDATING（处理中）→ PREVIEWED（通过）/ INVALID（行数据有误）/ FAILED（文件或处理失败）。VALIDATING 带 5 分钟租约，中断后过期任务可重新领取，迟到 Worker 不会覆盖已有结果。

数量按 CSV 数据行统计，商品文件一行对应一个 SKU，不应当作商品数量。管理员可查看本企业批次；主管可查看可访问店铺批次；运营人员仅查看自己上传且仍有店铺授权的批次。列表、详情、错误下载和 Worker 都检查实时权限。

原文件保存在 UPLOAD_DIR 私有目录（相对路径按 backend 解析），使用随机文件名；不公开服务器路径或原文件。Worker 复核 SHA-256 后读取。错误 CSV 对公式单元格转义。预检只保存批次、规范行、错误、版本依赖和脱敏审计。

预检通过后记录目标及引用版本和 30 分钟有效期，供后续确认时复核；当前 can_confirm=false，不提供确认写入端点。

## 接口及下一步

- GET `/api/v1/import-templates/{type}`：模板下载。
- POST `/api/v1/imports`：multipart 字段 type、store_id（店铺模板不传）、file；明确再次上传可加 allow_duplicate=true；返回 202。
- GET `/api/v1/imports?page=1`：批次列表，每页 20 条。
- GET `/api/v1/imports/{id}?page=1`：预检及逐行差异，每页 20 行。
- GET `/api/v1/imports/{id}/errors`：错误 CSV。

下一步实现预检令牌、版本及新增键复核、显式覆盖、幂等确认、单事务入库和日经营 batch_id 追溯。原文件 30 天、终态暂存行 7 天及批次/审计至少 180 天的清理任务尚未交付，当前不会自动清理。AI 任务执行尚未开放。
