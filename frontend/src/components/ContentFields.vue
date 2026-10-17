<script setup lang="ts">
import { computed } from 'vue'
export type Value = string | number | boolean | null | Value[] | { [key: string]: Value }
const props = defineProps<{ modelValue: Value; editable?: boolean; field?: string }>()
const emit = defineEmits<{ 'update:modelValue': [Value] }>()
const labels: Record<string, string> = {
  strengths: '潜在优势',
  issues: '待验证问题',
  actions: '行动建议',
  opportunities: '机会建议',
  risks: '风险假设',
  comparison_refs: '竞品引用',
  comparison_rows: '竞品来源资料',
  ideas: '主题创意',
  name: '名称',
  insight: '用户洞察',
  angle: '表达角度',
  direction: '创意方向',
  cta: '行动引导',
  shots: '分镜',
  start: '开始秒数',
  end: '结束秒数',
  visual: '画面',
  voiceover: '口播',
  subtitle: '字幕',
  props: '道具',
  closing: '收尾',
  allocations: '预算分配',
  channel: '渠道',
  amount: '金额（元）',
  percent: '占比（%）',
  tests: '测试建议',
  stop_rules: '停止规则',
  assumptions: '待验证假设',
  anomalies: '异常分析',
  metric_ref: '指标引用',
  reasoning: '原因假设',
  next_actions: '后续行动',
  ref: '来源引用',
  platform: '平台',
  spec: '规格',
  price: '价格',
  source_note: '来源说明',
  source_url: '来源地址',
  collected_at: '采集日期',
  price_comparable: '是否可比',
  version: '资料版本',
  id: '资料编号',
  brand: '品牌',
  selling_points: '卖点',
}
const locked = computed(
  () =>
    !props.editable ||
    ['comparison_rows', 'comparison_refs', 'channel', 'percent', 'metric_ref'].includes(
      props.field ?? '',
    ),
)
const entries = computed(() =>
  props.modelValue && typeof props.modelValue === 'object' ? Object.entries(props.modelValue) : [],
)
function update(key: string, value: Value) {
  if (Array.isArray(props.modelValue)) {
    const next = [...props.modelValue]
    next[Number(key)] = value
    emit('update:modelValue', next)
  } else if (props.modelValue && typeof props.modelValue === 'object') {
    emit('update:modelValue', { ...props.modelValue, [key]: value })
  }
}
</script>
<template>
  <div class="content-fields">
    <template v-if="modelValue !== null && typeof modelValue === 'object'">
      <section v-for="[key, value] in entries" :key="key" class="field-row">
        <strong>{{
          Array.isArray(modelValue) ? `第 ${Number(key) + 1} 项` : (labels[key] ?? '资料')
        }}</strong>
        <ContentFields
          :model-value="value"
          :editable="!locked"
          :field="Array.isArray(modelValue) ? field : key"
          @update:model-value="update(key, $event)"
        />
      </section>
    </template>
    <el-input
      v-else-if="!locked && typeof modelValue === 'string'"
      :model-value="modelValue"
      type="textarea"
      :autosize="{ minRows: 2, maxRows: 8 }"
      maxlength="2000"
      @update:model-value="emit('update:modelValue', $event)"
    />
    <el-input-number
      v-else-if="!locked && typeof modelValue === 'number'"
      :model-value="modelValue"
      :min="0"
      :max="60"
      :precision="0"
      @update:model-value="emit('update:modelValue', $event ?? 0)"
    />
    <p v-else>
      {{
        modelValue === null
          ? 'N/A'
          : modelValue === true
            ? '是'
            : modelValue === false
              ? '否'
              : modelValue
      }}
    </p>
  </div>
</template>
<style scoped>
.field-row {
  margin: 10px 0;
  padding-left: 12px;
  border-left: 2px solid #e6eee9;
}
strong {
  display: block;
  font-size: 13px;
  color: #506359;
  margin-bottom: 6px;
}
p {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  margin: 6px 0;
}
</style>
