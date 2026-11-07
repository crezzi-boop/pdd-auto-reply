// 默认回复与商品专属回复接口模块（拼多多自动回复系统前端）
// 职责（需求 7.1/7.2/7.3/7.4/7.5）：封装 backend 「默认回复」与「商品专属回复」REST 接口调用。
// 说明：统一经 @/utils/request 发起，成功返回后端 data，失败已统一 showToast 提示；
//       后端地址经环境变量配置（禁止写死 localhost，规范 21）。
import { get, post, put, del } from '@/utils/request'

// -------------------- 默认回复（按店铺一条配置，可含多条语句） --------------------

// 查询某店铺默认回复配置（返回 contents 多条数组 + rotate_minutes 轮换窗口）
export function fetchDefaultReply(shopPk) {
  return get('/default-replies', { shop_pk: shopPk })
}

// 保存（upsert）某店铺默认回复配置
// payload: { shop_pk, contents: string[], content?, rotate_minutes?, enabled?, reply_once? }
export function saveDefaultReply(payload) {
  return put('/default-replies', payload)
}

// 启用 / 停用某店铺默认回复
export function setDefaultReplyStatus(shopPk, enabled) {
  return put('/default-replies/status', { shop_pk: shopPk, enabled })
}

// -------------------- 商品专属回复（绑定 goods_id，优先级高于默认） --------------------

// 查询某店铺商品专属回复列表（后端分页，需求 7.5）
// @param {{shop_pk:number, page:number, page_size:number, enabled?:boolean}} params
export function fetchGoodsReplies(params) {
  return get('/goods-replies', params)
}

// 新增 / upsert 商品专属回复（需求 7.3）
// payload: { shop_pk, goods_id, reply_content, reply_type?, enabled? }
export function createGoodsReply(payload) {
  return post('/goods-replies', payload)
}

// 更新商品专属回复（仅更新传入的非空字段）
// payload: { reply_content?, reply_type?, enabled? }
export function updateGoodsReply(replyId, payload) {
  return put(`/goods-replies/${replyId}`, payload)
}

// 启用 / 停用商品专属回复
export function setGoodsReplyStatus(replyId, enabled) {
  return put(`/goods-replies/${replyId}/status`, { enabled })
}

// 逻辑删除商品专属回复（禁止物理删除，需求 24.6）
export function deleteGoodsReply(replyId) {
  return del(`/goods-replies/${replyId}`)
}
