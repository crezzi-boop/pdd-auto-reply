// 系统设置接口模块（拼多多自动回复系统前端）
// 职责：封装系统设置相关后端接口调用（需求 21.1/21.6-21.16），覆盖主题、分页、
//       基础设置、登录页品牌、免责声明、联系二维码、SMTP、代理、备份。
// 说明：统一经 @/utils/request 发起，后端地址经环境变量配置（禁止写死 localhost，规范 21）；
//       系统设置接口为管理员专属，非管理员调用由后端返回「无访问权限」（需求 21.17）。
import { get, post, put } from '@/utils/request'

// 系统设置总览
export function getAllSettings() {
  return get('/settings')
}

// 主题外观
export function getTheme() {
  return get('/settings/theme')
}
export function updateTheme(payload) {
  return put('/settings/theme', payload)
}

// 分页默认值
export function getPagination() {
  return get('/settings/pagination')
}
export function updatePagination(defaultPageSize) {
  return put('/settings/pagination', { default_page_size: defaultPageSize })
}

// 基础设置
export function getBasic() {
  return get('/settings/basic')
}
export function updateBasic(payload) {
  return put('/settings/basic', payload)
}

// 登录页品牌
export function getBrand() {
  return get('/settings/brand')
}
export function updateBrand(payload) {
  return put('/settings/brand', payload)
}

// 免责声明
export function getDisclaimer() {
  return get('/settings/disclaimer')
}
export function updateDisclaimer(payload) {
  return put('/settings/disclaimer', payload)
}

// 联系二维码
export function getQrcodes() {
  return get('/settings/qrcodes')
}
export function updateQrcodes(items) {
  return put('/settings/qrcodes', { items })
}

// SMTP 邮件设置
export function getSmtp() {
  return get('/settings/smtp')
}
export function updateSmtp(payload) {
  return put('/settings/smtp', payload)
}
export function sendTestEmail(payload) {
  return post('/settings/smtp/test', payload)
}

// 代理设置
export function getProxy() {
  return get('/settings/proxy')
}
export function updateProxy(payload) {
  return put('/settings/proxy', payload)
}

// 登录态自动维护（自动保活开关 + 每天维护时间）
export function getLoginMaintenance() {
  return get('/settings/login-maintenance')
}
export function updateLoginMaintenance(payload) {
  return put('/settings/login-maintenance', payload)
}
