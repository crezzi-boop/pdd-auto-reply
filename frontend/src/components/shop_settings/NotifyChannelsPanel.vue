<!--
  通知渠道面板（店铺管理「设置」弹窗内嵌，需求 18.1 / 18.2 / 18.5 配套）
  职责：按店铺维度管理通知渠道（渠道类型 + 目标地址 + 启停用），支持新增 / 编辑 / 测试发送。
  说明：店铺由父级弹窗经 shopPk 传入；渠道为店铺级（方案 A），系统事件仅推送本店铺已启用渠道；
        新增/编辑弹窗仅关闭按钮关闭（规范 7）；用户输入经 SafeHtml 转义（规范 22）、文案全中文（规范 27）。
-->
<script setup>
import { reactive, ref, computed, onMounted } from 'vue'
import {
  Loading,
  TableContainer,
  Pagination,
  FormModal,
  Select,
  SafeHtml,
} from '@/components/common'
import { showToast, TOAST_TYPE } from '@/utils/toast'
import { formatDateTime } from '@/utils/format'
import {
  fetchNotifyChannels,
  createNotifyChannel,
  updateNotifyChannel,
  testNotifyChannel,
  fetchChannelTypes,
} from '@/api/notify_api'

const props = defineProps({
  shopPk: {
    type: [Number, String],
    default: '',
  },
})

const loading = ref(false)
const submitting = ref(false)

// 渠道类型枚举：key -> 中文文案
const channelTypeMap = ref({})
const channelTypeOptions = ref([])

// 列表与分页
const list = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)

// 新增 / 编辑弹窗
const editVisible = ref(false)
const editingId = ref(null)
const form = reactive({ channel_type: '', target: '', enabled: true, keyword: '', secret: '' })

// 是否飞书机器人：专用类型，或历史「Webhook」类型但填的是飞书机器人地址
const isFeishu = computed(() => (
  form.channel_type === 'feishu' ||
  /open\.feishu\.cn|open\.larksuite\.com/i.test(form.target || '')
))

// 渠道类型中文展示
function channelTypeLabel(key) {
  if (!key) {
    return '—'
  }
  return channelTypeMap.value[key] || key
}

// 拉取渠道类型枚举字典
async function loadChannelTypes() {
  const data = await fetchChannelTypes().catch(() => null)
  const items = Array.isArray(data) ? data : []
  const map = {}
  const options = []
  for (const item of items) {
    map[item.key] = item.label
    options.push({ value: item.key, label: item.label })
  }
  channelTypeMap.value = map
  channelTypeOptions.value = options
}

// 拉取本店铺渠道列表
async function loadChannels() {
  if (!props.shopPk) {
    list.value = []
    total.value = 0
    return
  }
  loading.value = true
  try {
    const data = await fetchNotifyChannels({
      shop_pk: props.shopPk,
      page: page.value,
      page_size: pageSize.value,
    })
    list.value = (data && data.list) || []
    total.value = (data && data.total) || 0
  } catch (e) {
    // 失败已统一提示
  } finally {
    loading.value = false
  }
}

// 打开新增弹窗（默认取第一个渠道类型）
function openCreate() {
  if (!props.shopPk) {
    showToast('请先选择店铺', TOAST_TYPE.WARNING)
    return
  }
  editingId.value = null
  form.channel_type = channelTypeOptions.value.length > 0 ? channelTypeOptions.value[0].value : ''
  form.target = ''
  form.keyword = ''
  form.secret = ''
  form.enabled = true
  editVisible.value = true
}

// 打开编辑弹窗
function openEdit(row) {
  editingId.value = row.id
  form.channel_type = row.channel_type
  form.target = row.target
  form.keyword = row.keyword || ''
  form.secret = row.secret || ''
  form.enabled = Boolean(row.enabled)
  editVisible.value = true
}

// 保存渠道（新增或编辑）
async function onConfirm() {
  if (!form.channel_type) {
    showToast('请选择渠道类型', TOAST_TYPE.WARNING)
    return
  }
  if (!form.target || !form.target.trim()) {
    showToast('请填写通知目标地址', TOAST_TYPE.WARNING)
    return
  }
  submitting.value = true
  try {
    if (editingId.value === null) {
      await createNotifyChannel({
        shop_pk: props.shopPk,
        channel_type: form.channel_type,
        target: form.target.trim(),
        enabled: form.enabled,
        keyword: form.keyword.trim(),
        secret: form.secret.trim(),
      })
      showToast('创建成功', TOAST_TYPE.SUCCESS)
      page.value = 1
    } else {
      await updateNotifyChannel(editingId.value, {
        channel_type: form.channel_type,
        target: form.target.trim(),
        enabled: form.enabled,
        keyword: form.keyword.trim(),
        secret: form.secret.trim(),
      })
      showToast('更新成功', TOAST_TYPE.SUCCESS)
    }
    editVisible.value = false
    await loadChannels()
  } catch (e) {
    // 失败已统一提示
  } finally {
    submitting.value = false
  }
}

// 对某渠道发起测试发送
async function onTest(row) {
  submitting.value = true
  try {
    const result = await testNotifyChannel(row.id, {})
    showToast((result && result.detail) || '测试通知已发送', TOAST_TYPE.SUCCESS)
  } catch (e) {
    // 失败已统一提示
  } finally {
    submitting.value = false
  }
}

function onPageChange(value) {
  page.value = value
  loadChannels()
}
function onPageSizeChange(value) {
  pageSize.value = value
  page.value = 1
  loadChannels()
}

onMounted(async () => {
  await loadChannelTypes()
  await loadChannels()
})
</script>

<template>
  <div class="panel">
    <p class="panel__desc">配置本店铺的通知渠道（邮件 / Webhook / 企业微信 / 飞书机器人）；连接断开、登录态失效、风控触发等事件将推送至本店铺的已启用渠道。</p>
    <p class="panel__desc">使用飞书机器人时，请选择「飞书机器人」类型并填写群机器人的 Webhook 地址；若飞书后台启用了「自定义关键词」，请把同一个关键词填到「飞书关键词」里（留空默认用「提醒」），否则飞书会拦截消息。</p>

    <div class="panel__toolbar">
      <button class="btn btn--primary" type="button" @click="openCreate">新增渠道</button>
    </div>

    <div class="block__body">
      <Loading :visible="loading" text="加载中..." />
      <TableContainer max-height="320px">
        <table class="data-table">
          <thead>
            <tr>
              <th>渠道类型</th>
              <th>目标地址</th>
              <th>飞书关键词</th>
              <th>状态</th>
              <th>创建时间</th>
              <th class="col-actions">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in list" :key="row.id">
              <td>{{ channelTypeLabel(row.channel_type) }}</td>
              <td class="data-table__content"><SafeHtml :content="row.target || '—'" /></td>
              <td>{{ row.keyword || '—' }}</td>
              <td>
                <span class="status" :class="row.enabled ? 'status--on' : 'status--off'">
                  {{ row.enabled ? '已启用' : '已停用' }}
                </span>
              </td>
              <td>{{ formatDateTime(row.created_at) }}</td>
              <td class="col-actions">
                <button class="link" type="button" @click="openEdit(row)">编辑</button>
                <button class="link" type="button" :disabled="submitting" @click="onTest(row)">测试发送</button>
              </td>
            </tr>
            <tr v-if="!loading && list.length === 0">
              <td colspan="6" class="empty">暂无通知渠道</td>
            </tr>
          </tbody>
        </table>
      </TableContainer>
      <Pagination
        :page="page"
        :page-size="pageSize"
        :total="total"
        @update:page="onPageChange"
        @update:page-size="onPageSizeChange"
      />
    </div>

    <!-- 新增 / 编辑渠道弹窗 -->
    <FormModal
      v-model="editVisible"
      :title="editingId === null ? '新增通知渠道' : '编辑通知渠道'"
      :loading="submitting"
      confirm-text="保存"
      @confirm="onConfirm"
    >
      <div class="modal-form">
        <label class="field">
          <span class="field__label">渠道类型</span>
          <Select v-model="form.channel_type" :options="channelTypeOptions" placeholder="请选择渠道类型" />
        </label>
        <label class="field">
          <span class="field__label">目标地址</span>
          <input v-model="form.target" type="text" class="field__input" placeholder="邮箱 / Webhook URL（飞书：https://open.feishu.cn/open-apis/bot/v2/hook/...）" />
        </label>
        <template v-if="isFeishu">
          <label class="field">
            <span class="field__label">飞书关键词（自定义关键词）</span>
            <input v-model="form.keyword" type="text" class="field__input" placeholder="与飞书机器人安全设置一致，如：提醒（留空默认「提醒」）" />
            <span class="field__hint">飞书要求消息包含该关键词，系统会自动加到每条通知前面</span>
          </label>
          <label class="field">
            <span class="field__label">飞书签名密钥（选填）</span>
            <input v-model="form.secret" type="text" class="field__input" placeholder="仅在飞书后台启用「签名校验」时填写" />
          </label>
        </template>
        <label class="field field--inline">
          <input v-model="form.enabled" type="checkbox" />
          <span class="field__label">启用该渠道</span>
        </label>
      </div>
    </FormModal>
  </div>
</template>

<style scoped>
@import '@/components/shop_settings/panel.css';

.block__body {
  position: relative;
}
.modal-form {
  display: flex;
  flex-direction: column;
}
.field__hint {
  margin-top: 4px;
  font-size: 12px;
  opacity: 0.68;
  line-height: 1.5;
}
</style>
