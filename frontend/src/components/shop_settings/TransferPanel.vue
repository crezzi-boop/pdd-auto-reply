<!--
  转人工设置面板（店铺管理「设置」弹窗内嵌，需求 16.1）
  职责：按店铺维度展示可分配人工客服，并维护转人工关键词（新增、列表后端分页、启用/停用）。
  说明：店铺由父级弹窗经 shopPk 传入；新增弹窗仅关闭按钮关闭（规范 7）；
        用户输入经 SafeHtml 转义（规范 22）、文案全中文（规范 27）。
-->
<script setup>
import { reactive, ref, onMounted } from 'vue'
import {
  TableContainer,
  Pagination,
  Loading,
  FormModal,
  SafeHtml,
} from '@/components/common'
import { showToast, TOAST_TYPE } from '@/utils/toast'
import { autoReplyApi } from '@/api'

const props = defineProps({
  shopPk: {
    type: [Number, String],
    default: '',
  },
})

// 客服列表
const csList = ref([])
const csLoading = ref(false)

// 转人工关键词列表与分页
const keywordList = ref([])
const keywordTotal = ref(0)
const keywordPage = ref(1)
const keywordPageSize = ref(20)
const keywordLoading = ref(false)

// 新增关键词弹窗
const modalVisible = ref(false)
const saving = ref(false)
const form = reactive({ keyword: '' })

// 拉取客服列表（需求 16.1）
async function loadCsList() {
  if (!props.shopPk) {
    csList.value = []
    return
  }
  csLoading.value = true
  const data = await autoReplyApi.listCsList(props.shopPk).catch(() => null)
  csLoading.value = false
  if (!data) {
    csList.value = []
    return
  }
  csList.value = Array.isArray(data) ? data : Array.isArray(data.list) ? data.list : []
}

// 拉取转人工关键词列表（后端分页）
async function loadKeywords() {
  if (!props.shopPk) {
    keywordList.value = []
    keywordTotal.value = 0
    return
  }
  keywordLoading.value = true
  const data = await autoReplyApi
    .listTransferKeywords({ shop_pk: props.shopPk, page: keywordPage.value, page_size: keywordPageSize.value })
    .catch(() => null)
  keywordLoading.value = false
  if (!data) {
    return
  }
  keywordList.value = Array.isArray(data.list) ? data.list : []
  keywordTotal.value = data.total || 0
}

function openCreate() {
  if (!props.shopPk) {
    showToast('请先选择店铺', TOAST_TYPE.WARNING)
    return
  }
  form.keyword = ''
  modalVisible.value = true
}

async function submit() {
  if (!form.keyword.trim()) {
    showToast('请填写转人工关键词', TOAST_TYPE.WARNING)
    return
  }
  saving.value = true
  const result = await autoReplyApi
    .createTransferKeyword({ shop_pk: props.shopPk, keyword: form.keyword.trim(), enabled: true })
    .catch(() => null)
  saving.value = false
  if (result === null) {
    return
  }
  showToast('新增成功', TOAST_TYPE.SUCCESS)
  modalVisible.value = false
  keywordPage.value = 1
  loadKeywords()
}

async function toggleStatus(row) {
  const nextEnabled = !(row.enabled !== false)
  const result = await autoReplyApi.setTransferKeywordStatus(row.id, nextEnabled).catch(() => null)
  if (result === null) {
    return
  }
  showToast(nextEnabled ? '已启用' : '已停用', TOAST_TYPE.SUCCESS)
  loadKeywords()
}

function onPageChange(value) {
  keywordPage.value = value
  loadKeywords()
}
function onPageSizeChange(value) {
  keywordPageSize.value = value
  keywordPage.value = 1
  loadKeywords()
}

// ======================== 人工介入与回复保底设置 ========================
// 两个秒值按店铺保存；90 秒强制保底是后端硬约束，页面只做展示、不可关闭。
const guardDeadlineSeconds = 90
const guardForm = reactive({
  manual_delay_seconds: 30,
  manual_recover_seconds: 120,
})
const guardLoading = ref(false)
const guardSaving = ref(false)
const delayQuickValues = [0, 15, 30, 45, 60]
const recoverQuickValues = [30, 60, 120, 300, 0]

async function loadGuard() {
  if (!props.shopPk) {
    return
  }
  guardLoading.value = true
  const data = await autoReplyApi.getReplyGuard(props.shopPk).catch(() => null)
  guardLoading.value = false
  if (!data) {
    return
  }
  guardForm.manual_delay_seconds = Number(data.manual_delay_seconds ?? 30)
  guardForm.manual_recover_seconds = Number(data.manual_recover_seconds ?? 120)
}

// 前端即时校验：与后端范围保持一致，避免用户输入越界秒数导致回复越过保底。
function validateGuardForm() {
  const delay = guardForm.manual_delay_seconds
  const recover = guardForm.manual_recover_seconds
  if (delay === '' || delay === null || !Number.isInteger(Number(delay)) || Number(delay) < 0 || Number(delay) > 60) {
    showToast('机器延迟回复时间须为 0~60 秒的整数', TOAST_TYPE.WARNING)
    return null
  }
  if (recover === '' || recover === null || !Number.isInteger(Number(recover))) {
    showToast('自动恢复时长须为整数秒数', TOAST_TYPE.WARNING)
    return null
  }
  const recoverValue = Number(recover)
  if (recoverValue !== 0 && (recoverValue < 30 || recoverValue > 3600)) {
    showToast('自动恢复时长须为 30~3600 秒，或填 0 表示不自动恢复', TOAST_TYPE.WARNING)
    return null
  }
  return {
    manual_delay_seconds: Number(delay),
    manual_recover_seconds: recoverValue,
  }
}

async function saveGuard() {
  if (!props.shopPk) {
    showToast('请先选择店铺', TOAST_TYPE.WARNING)
    return
  }
  const payload = validateGuardForm()
  if (!payload) {
    return
  }
  guardSaving.value = true
  const result = await autoReplyApi.saveReplyGuard(props.shopPk, payload).catch(() => null)
  guardSaving.value = false
  if (result === null) {
    return
  }
  guardForm.manual_delay_seconds = Number(result.manual_delay_seconds ?? payload.manual_delay_seconds)
  guardForm.manual_recover_seconds = Number(result.manual_recover_seconds ?? payload.manual_recover_seconds)
  showToast('回复保底设置已保存', TOAST_TYPE.SUCCESS)
}

onMounted(async () => {
  await Promise.all([loadCsList(), loadKeywords(), loadGuard()])
})
</script>

<template>
  <div class="panel">
    <p class="panel__desc">查看店铺可分配的人工客服，并配置触发转人工的关键词；人工介入期间机器延迟回复，但 90 秒内必有保底回复。</p>

    <!-- 人工介入与回复保底设置（两个秒值，按店铺保存） -->
    <section class="block guard-card">
      <div class="block__header">
        <h4 class="block__title">人工介入与回复保底</h4>
        <span v-if="guardLoading" class="guard-loading">加载中...</span>
      </div>
      <div class="guard-grid">
        <div class="guard-field">
          <span class="guard-label">人工介入后机器延迟回复</span>
          <div class="guard-input">
            <input
              v-model.number="guardForm.manual_delay_seconds"
              type="number"
              min="0"
              max="60"
              step="1"
              class="field__input"
            />
            <span class="guard-unit">秒</span>
          </div>
          <span class="guard-hint">0~60 秒，默认 30 秒。人工接手后机器先等这么久；客服回过就取消自动回复。</span>
          <div class="guard-quick">
            <button
              v-for="value in delayQuickValues"
              :key="`d-${value}`"
              type="button"
              class="guard-chip"
              :class="{ 'guard-chip--active': guardForm.manual_delay_seconds === value }"
              @click="guardForm.manual_delay_seconds = value"
            >{{ value }} 秒</button>
          </div>
        </div>
        <div class="guard-field">
          <span class="guard-label">人工介入自动恢复时长</span>
          <div class="guard-input">
            <input
              v-model.number="guardForm.manual_recover_seconds"
              type="number"
              min="0"
              max="3600"
              step="1"
              class="field__input"
            />
            <span class="guard-unit">秒</span>
          </div>
          <span class="guard-hint">30~3600 秒，默认 120 秒。按每个客户独立计时，从最后一次人工消息起算，客服每回一句重新计时；填 0 表示不自动恢复。</span>
          <div class="guard-quick">
            <button
              v-for="value in recoverQuickValues"
              :key="`r-${value}`"
              type="button"
              class="guard-chip"
              :class="{ 'guard-chip--active': guardForm.manual_recover_seconds === value }"
              @click="guardForm.manual_recover_seconds = value"
            >{{ value === 0 ? '不自动恢复' : value + ' 秒' }}</button>
          </div>
        </div>
      </div>
      <div class="guard-footer">
        <span class="guard-note">硬约束：客户消息发出后 {{ guardDeadlineSeconds }} 秒内必有一条默认回复保底，不受人工介入、AI 开关、全店暂停、风控与过滤规则影响，且不可关闭。</span>
        <button
          class="btn btn--primary"
          type="button"
          :disabled="guardSaving || !shopPk"
          @click="saveGuard"
        >{{ guardSaving ? '保存中...' : '保存设置' }}</button>
      </div>
    </section>

    <!-- 可分配客服列表 -->
    <section class="block">
      <h4 class="block__title">可分配人工客服</h4>
      <div class="cs-wrap">
        <Loading :visible="csLoading" text="加载中..." />
        <div v-if="csList.length === 0 && !csLoading" class="cs-empty">暂无可分配客服</div>
        <ul v-else class="cs-list">
          <li v-for="cs in csList" :key="cs.cs_uid || cs.uid || cs.id" class="cs-item">
            <span class="cs-name"><SafeHtml :content="cs.cs_name || cs.name || '未命名客服'" /></span>
            <span class="cs-uid"><SafeHtml :content="cs.cs_uid || cs.uid || ''" /></span>
          </li>
        </ul>
      </div>
    </section>

    <!-- 转人工关键词 -->
    <section class="block">
      <div class="block__header">
        <h4 class="block__title">转人工关键词</h4>
        <button class="btn btn--primary block__action" type="button" @click="openCreate">新增关键词</button>
      </div>
      <div class="block__body">
        <Loading :visible="keywordLoading" text="加载中..." />
        <TableContainer max-height="280px">
          <table class="data-table">
            <thead>
              <tr>
                <th>关键词</th>
                <th>状态</th>
                <th class="col-actions">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="keywordList.length === 0">
                <td colspan="3" class="empty">暂无数据</td>
              </tr>
              <tr v-for="row in keywordList" :key="row.id">
                <td><SafeHtml :content="row.keyword" /></td>
                <td>
                  <span class="status" :class="row.enabled !== false ? 'status--on' : 'status--off'">
                    {{ row.enabled !== false ? '启用' : '停用' }}
                  </span>
                </td>
                <td class="col-actions">
                  <button class="link" type="button" @click="toggleStatus(row)">
                    {{ row.enabled !== false ? '停用' : '启用' }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </TableContainer>
        <Pagination
          :page="keywordPage"
          :page-size="keywordPageSize"
          :total="keywordTotal"
          @update:page="onPageChange"
          @update:page-size="onPageSizeChange"
        />
      </div>
    </section>

    <!-- 新增关键词弹窗 -->
    <FormModal
      v-model="modalVisible"
      title="新增转人工关键词"
      :loading="saving"
      confirm-text="保存"
      @confirm="submit"
    >
      <label class="field">
        <span class="field__label">关键词</span>
        <input v-model="form.keyword" class="field__input" type="text" placeholder="如：人工、投诉、转客服" />
      </label>
    </FormModal>
  </div>
</template>

<style scoped>
@import '@/components/shop_settings/panel.css';

.guard-card {
  padding: 16px;
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border);
  border-radius: 8px;
}
.guard-loading {
  margin-left: auto;
  font-size: 12px;
  color: var(--color-text-secondary);
}
.guard-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.guard-field {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.guard-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
}
.guard-input {
  display: flex;
  align-items: center;
  gap: 8px;
}
.guard-input .field__input {
  width: 120px;
}
.guard-unit {
  font-size: 13px;
  color: var(--color-text-secondary);
}
.guard-hint {
  font-size: 12px;
  line-height: 1.6;
  color: var(--color-text-secondary);
}
.guard-quick {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.guard-chip {
  padding: 4px 10px;
  font-size: 12px;
  color: var(--color-text);
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  cursor: pointer;
}
.guard-chip--active {
  color: #1677ff;
  border-color: #1677ff;
}
.guard-footer {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-top: 16px;
}
.guard-note {
  flex: 1;
  font-size: 12px;
  line-height: 1.6;
  color: var(--color-text-secondary);
}
@media (max-width: 720px) {
  .guard-grid {
    grid-template-columns: 1fr;
  }
  .guard-footer {
    flex-direction: column;
    align-items: flex-start;
  }
}

.block {
  margin-bottom: 20px;
}
.block__header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}
.block__title {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 10px;
}
.block__action {
  margin-left: auto;
}
.block__body {
  position: relative;
}
.cs-wrap {
  position: relative;
  min-height: 60px;
}
.cs-empty {
  font-size: 13px;
  color: var(--color-text-secondary);
  padding: 12px 0;
}
.cs-list {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  list-style: none;
}
.cs-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 14px;
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border);
  border-radius: 8px;
}
.cs-name {
  font-size: 14px;
  color: var(--color-text);
}
.cs-uid {
  font-size: 12px;
  color: var(--color-text-secondary);
}
</style>
