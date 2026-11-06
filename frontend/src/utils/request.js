// HTTP 请求统一封装（拼多多自动回复系统前端）
// 职责：
//   1. 统一创建 Axios 实例，请求超时统一为 90 秒（规范 19）；
//   2. 后端 API 基址经环境变量 VITE_API_BASE_URL 配置，禁止写死 localhost（规范 21 / 需求 25.4）；
//   3. 请求拦截器自动附加登录令牌（Authorization: Bearer <token>，从 localStorage 读取）；
//   4. 响应拦截器统一解析后端响应体 { code, success, message, data }：
//        - success=true 时直接返回 data 部分供调用方使用；
//        - success=false 时经 showToast 提示后端 message，并以一个「已处理」的拒绝结束，
//          前端不再 try/catch 二次包装错误，浏览器控制台不产生未处理报错（规范 2/4）；
//   5. 未登录 / 登录过期（后端业务码标识）时提示并引导重新登录。
// 依赖说明：showToast 当前为占位/轻量实现（utils/toast.js），任务 16.3 实现正式 Toast 组件后
//          经 registerToastHandler 注入正式实现即可，本文件无需改动。
import axios from 'axios'
import { showToast, TOAST_TYPE } from '@/utils/toast'

// 请求超时统一为 90 秒（规范 19）
const REQUEST_TIMEOUT = 90000

// 后端 API 基址经环境变量配置，未提供时回退为相对路径 /api（经 nginx/vite 代理，禁止写死 localhost）
const baseURL = import.meta.env.VITE_API_BASE_URL || '/api/v1'

// 登录令牌在 localStorage 中的键名（与登录模块保持一致）
const TOKEN_STORAGE_KEY = 'auth_token'

// 标识「未登录 / 登录已过期」的后端业务码（与 backend 鉴权依赖约定一致，需求 1.4 / 设计统一响应体）
const UNAUTHENTICATED_CODE = 40100

// 登录页路由路径（任务 17.1 实现登录页后跳转至此）
const LOGIN_PATH = '/login'

// 创建统一 Axios 实例
const request = axios.create({
  baseURL,
  timeout: REQUEST_TIMEOUT,
  headers: {
    'Content-Type': 'application/json',
  },
})

// 读取登录令牌
function getToken() {
  return localStorage.getItem(TOKEN_STORAGE_KEY)
}

// 防止过期后重复弹出多条「请重新登录」提示与多次跳转
let hasRedirectedToLogin = false

// 引导用户重新登录：清除本地令牌并跳转登录页（一次会话仅触发一次）
function redirectToLogin() {
  if (hasRedirectedToLogin) {
    return
  }
  hasRedirectedToLogin = true
  localStorage.removeItem(TOKEN_STORAGE_KEY)
  // 已在登录页则不再跳转，避免循环
  if (window.location.pathname !== LOGIN_PATH) {
    // 记录原访问地址，登录后可回跳（登录页按需读取）
    const redirect = encodeURIComponent(window.location.pathname + window.location.search)
    window.location.href = `${LOGIN_PATH}?redirect=${redirect}`
  }
}

// 请求拦截器：自动附加登录令牌
request.interceptors.request.use(
  (config) => {
    const token = getToken()
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    // FormData 由浏览器自动设置 multipart/form-data + boundary，不强制 JSON
    if (config.data instanceof FormData) {
      delete config.headers['Content-Type']
    }
    return config
  },
  // 请求阶段异常：提示网络错误并结束（前端不再二次包装）
  (error) => {
    showToast('请求发送失败，请检查网络后重试', TOAST_TYPE.ERROR)
    return Promise.reject(error)
  }
)

// 响应拦截器：统一解析响应体，按 success/message 处理
request.interceptors.response.use(
  (response) => {
    const body = response.data

    // 兜底：响应体非统一结构（如下载二进制流）时，原样返回 data
    if (!body || typeof body !== 'object' || typeof body.success === 'undefined') {
      return body
    }

    // 业务成功：返回 data 部分供调用方直接使用
    if (body.success) {
      return body.data
    }

    // 未登录 / 登录已过期：提示并引导重新登录
    if (body.code === UNAUTHENTICATED_CODE) {
      showToast(body.message || '登录已过期，请重新登录', TOAST_TYPE.WARNING)
      redirectToLogin()
      // 以「已处理」的拒绝结束，避免调用方再次提示（控制台不出现未处理报错）
      return Promise.reject({ handled: true, code: body.code, message: body.message })
    }

    // 其它业务失败：直接展示后端 message（规范 2/4，前端不再二次包装）
    showToast(body.message || '操作失败，请稍后重试', TOAST_TYPE.ERROR)
    return Promise.reject({ handled: true, code: body.code, message: body.message })
  },
  // 网络层 / HTTP 层异常：统一提示（后端业务错误恒返回 200，此处主要为网络或服务不可用）
  (error) => {
    // 已被「已处理」拒绝包装的错误直接透传，避免重复提示
    if (error && error.handled) {
      return Promise.reject(error)
    }
    let message = '网络异常或服务不可用，请稍后重试'
    if (error && error.code === 'ECONNABORTED') {
      message = '请求超时，请稍后重试'
    }
    showToast(message, TOAST_TYPE.ERROR)
    return Promise.reject({ handled: true, message })
  }
)

// 封装常用请求方法：成功时 resolve 后端 data，失败已统一提示，调用方按需 await
export function get(url, params, config = {}) {
  return request.get(url, { params, ...config })
}

export function post(url, data, config = {}) {
  return request.post(url, data, config)
}

export function put(url, data, config = {}) {
  return request.put(url, data, config)
}

export function del(url, config = {}) {
  return request.delete(url, config)
}

export function patch(url, data, config = {}) {
  return request.patch(url, data, config)
}

export default request
