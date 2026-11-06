<!--
  系统设置页面（需求 21.1/21.6-21.15/21.17）
  职责：以卡片形式集中管理全套系统设置——主题外观、分页默认值、基础设置、
        登录页品牌、免责声明、联系二维码、SMTP 邮件、代理设置。
  风格：与参考项目 xianyu-auto-reply-wangpan 一致——vben-card 卡片 + 卡片头图标标题 +
        input-ios 输入框 + switch-ios 开关 + btn-ios 按钮 + lucide 图标 + 页头。
  规范要点：
    - 系统设置仅管理员可见（路由 meta.adminOnly + 后端 21.17 双重保障）；
    - 加载与保存均展示遮罩 + 转圈（规范 23）；
    - 操作结果经 showToast 提示（规范 6），前端不二次包装错误（规范 2/4）；
    - 全中文、响应式布局（规范 27/20）。
-->
<script setup>
import { ref, reactive, onMounted } from 'vue'
import {
  Palette,
  List,
  Settings as SettingsIcon,
  Building2,
  FileText,
  QrCode,
  Mail,
  Globe,
  Plus,
  Trash2,
  Eye,
  EyeOff,
  RefreshCw,
} from 'lucide-vue-next'
import { Loading, Select } from '@/components/common'
import { showToast, TOAST_TYPE } from '@/utils/toast'
import {
  getTheme, updateTheme,
  getPagination, updatePagination,
  getBasic, updateBasic,
  getBrand, updateBrand,
  getDisclaimer, updateDisclaimer,
  getQrcodes, updateQrcodes,
  getSmtp, updateSmtp, sendTestEmail,
  getProxy, updateProxy,
  getLoginMaintenance, updateLoginMaintenance,
} from '@/api/settings_api'
import { THEME_COLOR_PRESETS } from '@/utils/theme'
import { useLayoutStore } from '@/store'

const uiStore = useLayoutStore()

const loading = ref(false)
const saving = reactive({})

const theme = reactive({ theme_color: '#1677ff', dark_mode: 'light', font_family: 'default' })
const pagination = reactive({ default_page_size: 20 })
const basic = reactive({ allow_register: false, show_default_login: false, enable_captcha: false, log_retention_days: 30 })
const brand = reactive({ system_name: '', title: '', description: '' })
const disclaimer = reactive({ title: '', content: '', checkbox_text: '', agree_text: '', disagree_text: '' })
const qrcodes = reactive({ items: [] })
const smtp = reactive({ host: '', port: 465, sender_email: '', sender_name: '', use_ssl: true, password: '', password_set: false })
// SMTP 密码是否明文显示（隐藏查看切换，参照 wangpan 系统设置）
const showSmtpPassword = ref(false)
const testEmail = ref('')
const proxy = reactive({ enabled: false, api_url: '' })
const loginMaintenance = reactive({ enabled: true, time: '08:00' })

const darkModeOptions = [
  { value: 'light', label: '亮色模式' },
  { value: 'dark', label: '暗色模式' },
  { value: 'auto', label: '跟随系统' },
]
const pageSizeOptions = [
  { value: 10, label: '10 条/页' },
  { value: 20, label: '20 条/页' },
  { value: 50, label: '50 条/页' },
  { value: 100, label: '100 条/页' },
]
const colorPresets = THEME_COLOR_PRESETS

function assign(target, source) {
  if (source && typeof source === 'object') {
    Object.keys(source).forEach((key) => {
      target[key] = source[key]
    })
  }
}

async function loadAll() {
  loading.value = true
  const [t, p, b, br, d, q, s, px, lm] = await Promise.allSettled([
    getTheme(), getPagination(), getBasic(), getBrand(), getDisclaimer(),
    getQrcodes(), getSmtp(), getProxy(), getLoginMaintenance(),
  ])
  if (t.status === 'fulfilled') assign(theme, t.value)
  if (p.status === 'fulfilled') assign(pagination, p.value)
  if (b.status === 'fulfilled') assign(basic, b.value)
  if (br.status === 'fulfilled') assign(brand, br.value)
  if (d.status === 'fulfilled') assign(disclaimer, d.value)
  if (q.status === 'fulfilled' && q.value) qrcodes.items = q.value.items || []
  if (s.status === 'fulfilled') assign(smtp, s.value)
  if (px.status === 'fulfilled') assign(proxy, px.value)
  if (lm.status === 'fulfilled') assign(loginMaintenance, lm.value)
  loading.value = false
}

onMounted(loadAll)

async function runSave(key, fn, successMsg) {
  saving[key] = true
  try {
    await fn()
    showToast(successMsg, TOAST_TYPE.SUCCESS)
  } finally {
    saving[key] = false
  }
}

// 将主题色十六进制值映射为预设 key（用于驱动前端主题色即时生效，规范 26）
function colorHexToKey(hex) {
  const matched = THEME_COLOR_PRESETS.find((preset) => preset.color === hex)
  return matched ? matched.key : 'blue'
}

// 应用主题到前端（主题色 + 暗黑模式即时生效，与顶栏主题切换保持一致）
function applyThemeLive() {
  uiStore.setThemeColor(colorHexToKey(theme.theme_color))
  // dark_mode：light/dark 直接应用；auto 跟随系统偏好
  if (theme.dark_mode === 'dark') {
    uiStore.setDarkMode(true)
  } else if (theme.dark_mode === 'light') {
    uiStore.setDarkMode(false)
  } else {
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
    uiStore.setDarkMode(prefersDark)
  }
}

function saveTheme() {
  return runSave('theme', async () => {
    await updateTheme({ ...theme })
    // 保存成功后即时应用到前端，确保所见即所得（规范 26）
    applyThemeLive()
  }, '主题设置已保存')
}
function savePagination() {
  return runSave('pagination', () => updatePagination(Number(pagination.default_page_size)), '分页设置已保存')
}
function saveBasic() {
  return runSave('basic', () => updateBasic({ ...basic, log_retention_days: Number(basic.log_retention_days) }), '基础设置已保存')
}
function saveLoginMaintenance() {
  return runSave('loginMaintenance', () => updateLoginMaintenance({ enabled: loginMaintenance.enabled, time: loginMaintenance.time }), '登录态维护设置已保存')
}
function saveBrand() {
  return runSave('brand', () => updateBrand({ ...brand }), '品牌信息已保存')
}
function saveDisclaimer() {
  return runSave('disclaimer', () => updateDisclaimer({ ...disclaimer }), '免责声明已保存')
}
function saveQrcodes() {
  return runSave('qrcodes', () => updateQrcodes(qrcodes.items), '二维码已保存')
}
function saveSmtp() {
  // 反显编辑：直接提交页面上的密码值（与所见一致）；留空表示清空已配置密码。
  const payload = {
    host: smtp.host,
    port: Number(smtp.port),
    sender_email: smtp.sender_email,
    sender_name: smtp.sender_name,
    use_ssl: smtp.use_ssl,
    password: smtp.password || '',
  }
  return runSave('smtp', async () => {
    const data = await updateSmtp(payload)
    if (data) assign(smtp, data)
  }, 'SMTP 设置已保存')
}
async function onSendTestEmail() {
  if (!testEmail.value.trim()) {
    showToast('请填写测试收件地址', TOAST_TYPE.WARNING)
    return
  }
  await runSave('testEmail', () => sendTestEmail({ to_email: testEmail.value.trim() }), '测试邮件已发送')
}
function saveProxy() {
  return runSave('proxy', () => updateProxy({ enabled: proxy.enabled, api_url: proxy.api_url }), '代理设置已保存')
}

function addQrcode() {
  qrcodes.items = [...qrcodes.items, { type: 'wechat', image_url: '' }]
}
function removeQrcode(index) {
  qrcodes.items = qrcodes.items.filter((_, i) => i !== index)
}
</script>

<template>
  <div class="relative">
    <!-- 页头 -->
    <div class="page-header">
      <h1 class="page-title">系统设置</h1>
      <p class="page-description">配置系统全局设置（仅管理员可见）</p>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <!-- 主题外观 -->
      <div class="vben-card">
        <div class="vben-card-header">
          <h2 class="vben-card-title"><Palette class="w-4 h-4" /> 主题外观</h2>
        </div>
        <div class="vben-card-body space-y-4">
          <div class="input-group">
            <label class="input-label">主题色</label>
            <div class="flex flex-wrap gap-2.5">
              <button
                v-for="preset in colorPresets"
                :key="preset.key"
                type="button"
                class="w-7 h-7 rounded-full border-2 transition-transform hover:scale-110"
                :class="theme.theme_color === preset.color ? 'border-slate-800 dark:border-white' : 'border-transparent'"
                :style="{ background: preset.color }"
                :title="preset.label"
                @click="theme.theme_color = preset.color"
              />
            </div>
          </div>
          <div class="input-group">
            <label class="input-label">明暗模式</label>
            <Select v-model="theme.dark_mode" :options="darkModeOptions" />
          </div>
          <div class="input-group">
            <label class="input-label">字体</label>
            <input v-model="theme.font_family" class="input-ios" placeholder="字体族名称" />
          </div>
          <button class="btn-ios-primary" :disabled="saving.theme" @click="saveTheme">保存主题</button>
        </div>
      </div>

      <!-- 分页默认值 -->
      <div class="vben-card">
        <div class="vben-card-header">
          <h2 class="vben-card-title"><List class="w-4 h-4" /> 分页默认值</h2>
        </div>
        <div class="vben-card-body space-y-4">
          <div class="input-group">
            <label class="input-label">默认每页条数</label>
            <Select v-model="pagination.default_page_size" :options="pageSizeOptions" />
          </div>
          <button class="btn-ios-primary" :disabled="saving.pagination" @click="savePagination">保存分页</button>
        </div>
      </div>

      <!-- 基础设置 -->
      <div class="vben-card">
        <div class="vben-card-header">
          <h2 class="vben-card-title"><SettingsIcon class="w-4 h-4" /> 基础设置</h2>
        </div>
        <div class="vben-card-body space-y-4">
          <div class="flex items-center justify-between py-2">
            <div>
              <p class="font-medium text-slate-900 dark:text-slate-100">允许用户注册</p>
              <p class="text-sm text-slate-500 dark:text-slate-400">开启后允许新用户注册账号</p>
            </div>
            <label class="switch-ios">
              <input type="checkbox" v-model="basic.allow_register" />
              <span class="switch-slider"></span>
            </label>
          </div>
          <div class="flex items-center justify-between py-2 border-t border-slate-100 dark:border-slate-700">
            <div>
              <p class="font-medium text-slate-900 dark:text-slate-100">显示默认登录信息</p>
              <p class="text-sm text-slate-500 dark:text-slate-400">登录页显示默认账号密码提示</p>
            </div>
            <label class="switch-ios">
              <input type="checkbox" v-model="basic.show_default_login" />
              <span class="switch-slider"></span>
            </label>
          </div>
          <div class="flex items-center justify-between py-2 border-t border-slate-100 dark:border-slate-700">
            <div>
              <p class="font-medium text-slate-900 dark:text-slate-100">启用登录验证码</p>
              <p class="text-sm text-slate-500 dark:text-slate-400">开启后登录需完成验证码校验</p>
            </div>
            <label class="switch-ios">
              <input type="checkbox" v-model="basic.enable_captcha" />
              <span class="switch-slider"></span>
            </label>
          </div>
          <div class="flex items-center justify-between py-2 border-t border-slate-100 dark:border-slate-700">
            <div>
              <p class="font-medium text-slate-900 dark:text-slate-100">日志保留天数</p>
              <p class="text-sm text-slate-500 dark:text-slate-400">所有模块日志保留天数（1~365 天）</p>
            </div>
            <input v-model="basic.log_retention_days" type="number" min="1" max="365" class="input-ios w-24 text-center" />
          </div>
          <button class="btn-ios-primary" :disabled="saving.basic" @click="saveBasic">保存基础设置</button>
        </div>
      </div>

      <!-- 登录态自动维护 -->
      <div class="vben-card">
        <div class="vben-card-header">
          <h2 class="vben-card-title"><RefreshCw class="w-4 h-4" /> 登录态自动维护</h2>
        </div>
        <div class="vben-card-body space-y-4">
          <div class="flex items-center justify-between py-2">
            <div>
              <p class="font-medium text-slate-900 dark:text-slate-100">自动保持登录（推荐开启）</p>
              <p class="text-sm text-slate-500 dark:text-slate-400">系统每 10 分钟自动检查一次店铺登录态，避免长时间无操作被踢下线</p>
            </div>
            <label class="switch-ios">
              <input type="checkbox" v-model="loginMaintenance.enabled" />
              <span class="switch-slider"></span>
            </label>
          </div>
          <div class="flex items-center justify-between py-2 border-t border-slate-100 dark:border-slate-700">
            <div>
              <p class="font-medium text-slate-900 dark:text-slate-100">每天自动维护时间</p>
              <p class="text-sm text-slate-500 dark:text-slate-400">到点额外做一次完整检查；若登录态已失效，会在日志里提示重新扫码</p>
            </div>
            <input v-model="loginMaintenance.time" type="time" class="input-ios w-32 text-center" />
          </div>
          <button class="btn-ios-primary" :disabled="saving.loginMaintenance" @click="saveLoginMaintenance">保存登录维护设置</button>
        </div>
      </div>

      <!-- 登录页品牌 -->
      <div class="vben-card">
        <div class="vben-card-header">
          <h2 class="vben-card-title"><Building2 class="w-4 h-4" /> 登录页品牌</h2>
        </div>
        <div class="vben-card-body space-y-4">
          <div class="input-group">
            <label class="input-label">系统名称</label>
            <input v-model="brand.system_name" class="input-ios" />
          </div>
          <div class="input-group">
            <label class="input-label">登录页标题</label>
            <input v-model="brand.title" class="input-ios" />
          </div>
          <div class="input-group">
            <label class="input-label">登录页描述</label>
            <input v-model="brand.description" class="input-ios" />
          </div>
          <button class="btn-ios-primary" :disabled="saving.brand" @click="saveBrand">保存品牌信息</button>
        </div>
      </div>

      <!-- 免责声明 -->
      <div class="vben-card">
        <div class="vben-card-header">
          <h2 class="vben-card-title"><FileText class="w-4 h-4" /> 免责声明</h2>
        </div>
        <div class="vben-card-body space-y-4">
          <div class="input-group">
            <label class="input-label">标题</label>
            <input v-model="disclaimer.title" class="input-ios" />
          </div>
          <div class="input-group">
            <label class="input-label">正文</label>
            <textarea v-model="disclaimer.content" class="input-ios" rows="4"></textarea>
          </div>
          <div class="input-group">
            <label class="input-label">勾选文案</label>
            <input v-model="disclaimer.checkbox_text" class="input-ios" />
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div class="input-group">
              <label class="input-label">同意按钮</label>
              <input v-model="disclaimer.agree_text" class="input-ios" />
            </div>
            <div class="input-group">
              <label class="input-label">不同意按钮</label>
              <input v-model="disclaimer.disagree_text" class="input-ios" />
            </div>
          </div>
          <button class="btn-ios-primary" :disabled="saving.disclaimer" @click="saveDisclaimer">保存免责声明</button>
        </div>
      </div>

      <!-- 联系二维码 -->
      <div class="vben-card">
        <div class="vben-card-header">
          <h2 class="vben-card-title"><QrCode class="w-4 h-4" /> 联系二维码</h2>
        </div>
        <div class="vben-card-body space-y-3">
          <div v-for="(qr, index) in qrcodes.items" :key="index" class="flex gap-2 items-center">
            <input v-model="qr.type" class="input-ios flex-1" placeholder="类型（wechat/qq/mp/telegram）" />
            <input v-model="qr.image_url" class="input-ios flex-[2]" placeholder="图片地址 URL" />
            <button type="button" class="btn-ios-danger btn-sm flex-shrink-0" @click="removeQrcode(index)">
              <Trash2 class="w-3.5 h-3.5" />
            </button>
          </div>
          <div class="flex gap-2">
            <button type="button" class="btn-ios-secondary" @click="addQrcode">
              <Plus class="w-4 h-4" /> 新增二维码
            </button>
            <button class="btn-ios-primary" :disabled="saving.qrcodes" @click="saveQrcodes">保存二维码</button>
          </div>
        </div>
      </div>

      <!-- SMTP 邮件 -->
      <div class="vben-card">
        <div class="vben-card-header">
          <h2 class="vben-card-title"><Mail class="w-4 h-4" /> SMTP 邮件</h2>
        </div>
        <div class="vben-card-body space-y-4">
          <div class="input-group">
            <label class="input-label">服务器</label>
            <input v-model="smtp.host" class="input-ios" placeholder="如 smtp.qq.com" />
          </div>
          <div class="grid grid-cols-2 gap-3 items-end">
            <div class="input-group">
              <label class="input-label">端口</label>
              <input v-model="smtp.port" type="number" class="input-ios" />
            </div>
            <label class="checkbox-label py-2.5">
              <input type="checkbox" class="checkbox-ios" v-model="smtp.use_ssl" /> 使用 SSL
            </label>
          </div>
          <div class="input-group">
            <label class="input-label">发件邮箱</label>
            <input v-model="smtp.sender_email" class="input-ios" />
          </div>
          <div class="input-group">
            <label class="input-label">发件人显示名</label>
            <input v-model="smtp.sender_name" class="input-ios" />
          </div>
          <div class="input-group">
            <label class="input-label">密码 / 授权码 {{ smtp.password_set ? '（已配置，可查看 / 修改）' : '' }}</label>
            <div class="relative">
              <input
                v-model="smtp.password"
                :type="showSmtpPassword ? 'text' : 'password'"
                class="input-ios pr-10"
                placeholder="输入密码或授权码（留空表示清空）"
                autocomplete="new-password"
              />
              <button
                type="button"
                class="absolute right-2 top-1/2 -translate-y-1/2 p-1.5 text-slate-400 hover:text-slate-600 dark:hover:text-slate-300 transition-colors"
                :title="showSmtpPassword ? '隐藏' : '显示'"
                @click="showSmtpPassword = !showSmtpPassword"
              >
                <EyeOff v-if="showSmtpPassword" class="w-4 h-4" />
                <Eye v-else class="w-4 h-4" />
              </button>
            </div>
          </div>
          <button class="btn-ios-primary" :disabled="saving.smtp" @click="saveSmtp">保存 SMTP</button>
          <div class="flex gap-2 items-end pt-2 border-t border-slate-100 dark:border-slate-700">
            <div class="input-group flex-1">
              <label class="input-label">测试收件地址</label>
              <input v-model="testEmail" class="input-ios" placeholder="输入邮箱后发送测试邮件" />
            </div>
            <button class="btn-ios-secondary" :disabled="saving.testEmail" @click="onSendTestEmail">发送测试邮件</button>
          </div>
        </div>
      </div>

      <!-- 代理设置 -->
      <div class="vben-card">
        <div class="vben-card-header">
          <h2 class="vben-card-title"><Globe class="w-4 h-4" /> 代理设置</h2>
        </div>
        <div class="vben-card-body space-y-4">
          <div class="flex items-center justify-between py-2">
            <div>
              <p class="font-medium text-slate-900 dark:text-slate-100">开启代理</p>
              <p class="text-sm text-slate-500 dark:text-slate-400">开启前请先填写代理 API 地址</p>
            </div>
            <label class="switch-ios">
              <input type="checkbox" v-model="proxy.enabled" />
              <span class="switch-slider"></span>
            </label>
          </div>
          <div class="input-group">
            <label class="input-label">代理 API 地址</label>
            <input v-model="proxy.api_url" class="input-ios" placeholder="开启代理前请先填写代理 API 的 URL" />
          </div>
          <button class="btn-ios-primary" :disabled="saving.proxy" @click="saveProxy">保存代理</button>
        </div>
      </div>
    </div>

    <!-- 加载遮罩 + 转圈（规范 23） -->
    <Loading :visible="loading" full-screen text="加载中..." />
  </div>
</template>
