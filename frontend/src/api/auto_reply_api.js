// 自动回复设置接口模块（拼多多自动回复系统前端）
// 职责：封装「AI 设置、营业时间、消息过滤/黑名单、风控管理、转人工设置」相关 REST 调用。
// 对应后端路由：
//   - 营业时间   backend/app/api/routes/business_hours.py（需求 11.1）
//   - 消息过滤   backend/app/api/routes/message_filters.py（需求 12.1/12.6）
//   - 黑名单     backend/app/api/routes/blacklist.py（需求 12.3/12.5/12.6）
//   - 风控管理   backend/app/api/routes/risk_control.py（需求 13.1/13.4）
//   - AI 设置    /shops/{shop_pk}/ai-config（需求 8.6，按店铺维度持久化大模型参数）
//   - 转人工     /shops/{shop_pk}/transfer-keywords、/shops/{shop_pk}/cs-list（需求 16.1）
// 说明：统一经 @/utils/request 发起请求，成功返回后端 data，失败已统一 showToast 提示；
//       后端业务前缀 /api/v1，基址 /api 由 request 提供；禁止写死 localhost（规范 21）。
import { get, post, put } from '@/utils/request'


// ======================== AI 设置（需求 8.6） ========================

// 查询某店铺的大模型参数配置（API 密钥不返回明文）。
export function getAiConfig(shopPk) {
  return get(`/shops/${shopPk}/ai-config`)
}

// 保存（upsert）某店铺的大模型参数（模型名称、API 密钥、API 地址、提示词、是否启用 AI）。
export function saveAiConfig(shopPk, payload) {
  return put(`/shops/${shopPk}/ai-config`, payload)
}

// 测试某店铺 AI 连接（按所选接口类型探测；密钥留空则用已保存密钥测试）。
// payload: { provider_type?, model_name?, api_base?, api_key? }
export function testAiConfig(shopPk, payload) {
  return post(`/shops/${shopPk}/ai-config/test`, payload)
}

// 获取某店铺 AI 可用模型列表（自动获取模型名称；密钥留空则用已保存密钥）。
// payload: { provider_type?, api_base?, api_key? }；返回 { models: [{ id, name }] }
export function fetchAiModels(shopPk, payload) {
  return post(`/shops/${shopPk}/ai-config/models`, payload)
}

// 查询 AI 接口类型枚举（key + 中文 label + 默认地址），供接口类型下拉展示。
export function fetchAiProviderTypes() {
  return get('/ai-provider-types')
}

// ======================== 营业时间（需求 11.1） ========================

// 查询某店铺营业时间配置；未配置返回 null。
export function getBusinessHours(shopPk) {
  return get(`/shops/${shopPk}/business-hours`)
}

// 配置 / 更新某店铺营业时间（起止时刻 HH:MM，可跨午夜）。
export function saveBusinessHours(shopPk, payload) {
  return put(`/shops/${shopPk}/business-hours`, payload)
}

// ======================== 消息过滤（需求 12.1/12.6） ========================

// 过滤规则列表（后端分页）。
export function listFilterRules(params) {
  return get(`/message-filters`, params)
}

// 创建过滤规则。
export function createFilterRule(payload) {
  return post(`/message-filters`, payload)
}

// 修改过滤规则。
export function updateFilterRule(ruleId, payload) {
  return put(`/message-filters/${ruleId}`, payload)
}

// 启用 / 停用过滤规则（停用即逻辑删除）。
export function setFilterRuleStatus(ruleId, enabled) {
  return put(`/message-filters/${ruleId}/status`, { enabled })
}

// ======================== 黑名单（需求 12.3/12.5/12.6） ========================

// 黑名单列表（后端分页）。
export function listBlacklist(params) {
  return get(`/blacklist`, params)
}

// 加入黑名单（幂等）。
export function addToBlacklist(payload) {
  return post(`/blacklist`, payload)
}

// 移出黑名单（逻辑失效，禁止物理删除）。
export function removeFromBlacklist(blacklistId) {
  return put(`/blacklist/${blacklistId}/remove`)
}

// ======================== 风控管理（需求 13.1/13.4） ========================

// 查询某店铺风控规则；未配置返回 null。
export function getRiskRule(shopPk) {
  return get(`/shops/${shopPk}/risk-rule`)
}

// 配置 / 更新某店铺风控规则（频率上限与统计窗口）。
export function saveRiskRule(shopPk, payload) {
  return put(`/shops/${shopPk}/risk-rule`, payload)
}

// 查询风控类型枚举（中文文案），供前端展示。
export function listRiskTypes() {
  return get(`/risk-types`)
}

// ======================== 转人工设置（需求 16.1） ========================

// 查询某店铺可分配的人工客服列表（客服标识与名称，需求 16.1）。
export function listCsList(shopPk) {
  return get(`/shops/${shopPk}/cs-list`)
}

// 转人工关键词列表（触发转人工的关键词配置）。
export function listTransferKeywords(params) {
  return get(`/transfer-keywords`, params)
}

// 新增转人工关键词。
export function createTransferKeyword(payload) {
  return post(`/transfer-keywords`, payload)
}

// 启用 / 停用转人工关键词。
export function setTransferKeywordStatus(keywordId, enabled) {
  return put(`/transfer-keywords/${keywordId}/status`, { enabled })
}

// ======================== 人工介入与回复保底设置 ========================

// 查询某店铺的两个秒值；未配置时后端返回默认值（延迟 30 秒 / 自动恢复 120 秒）。
export function getReplyGuard(shopPk) {
  return get(`/shops/${shopPk}/reply-guard`)
}

// 保存两个秒值。payload: { manual_delay_seconds, manual_recover_seconds }
export function saveReplyGuard(shopPk, payload) {
  return put(`/shops/${shopPk}/reply-guard`, payload)
}
