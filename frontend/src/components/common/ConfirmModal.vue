<!--
  通用确认弹窗组件（ConfirmModal）
  职责：替代浏览器原生 confirm，提供统一样式的确认/提示弹窗。
  关键规范：
    - 仅允许通过「关闭」按钮（右上角 X / 取消按钮 / 确定按钮）关闭弹窗；
    - 禁止点击遮罩或空白处关闭（规范 7 / 需求 23.12）——遮罩点击不触发任何关闭逻辑。
  用法（v-model 双向绑定显隐）：
    <ConfirmModal v-model="open" title="提示" message="确认删除？" @confirm="onConfirm" />
  说明：文案全中文，不依赖第三方图标库（内联 SVG）。
-->
<script setup>
const props = defineProps({
  // 是否显示（配合 v-model:modelValue）
  modelValue: {
    type: Boolean,
    default: false,
  },
  // 标题
  title: {
    type: String,
    default: '确认操作',
  },
  // 正文内容
  message: {
    type: String,
    default: '',
  },
  // 确定按钮文案
  confirmText: {
    type: String,
    default: '确定',
  },
  // 取消按钮文案
  cancelText: {
    type: String,
    default: '取消',
  },
  // 类型：warning / danger / info，用于图标与确定按钮配色
  type: {
    type: String,
    default: 'warning',
  },
  // 加载中（确认操作进行时禁用按钮并展示转圈）
  loading: {
    type: Boolean,
    default: false,
  },
  // 是否展示取消按钮（仅提示场景可隐藏，仅保留关闭/确定）
  showCancel: {
    type: Boolean,
    default: true,
  },
})

const emit = defineEmits(['update:modelValue', 'confirm', 'cancel'])

// 关闭弹窗（仅由按钮触发，遮罩点击不会调用）
function close() {
  if (props.loading) {
    return
  }
  emit('update:modelValue', false)
  emit('cancel')
}

// 点击确定
function confirm() {
  if (props.loading) {
    return
  }
  emit('confirm')
}

// 遮罩点击：按规范 7 显式不做任何关闭处理（保留空函数以表明刻意为之）
function onOverlayClick() {
  // 禁止点击遮罩关闭，故此处不执行任何操作
}
</script>

<template>
  <!--
    Teleport 到 body：避免弹窗被父级弹窗创建的 z-index 堆叠上下文困住，
    保证多层弹窗（如店铺设置弹窗内的删除确认）层级正确、可正常交互。
  -->
  <Teleport to="body">
    <transition name="modal-fade">
      <div v-if="modelValue" class="modal-root">
      <!-- 遮罩层：点击不关闭（规范 7） -->
      <div class="modal-overlay" @click="onOverlayClick"></div>

      <!-- 弹窗主体 -->
      <div class="modal-dialog" role="dialog" aria-modal="true">
        <!-- 右上角关闭按钮 -->
        <button class="modal-x" type="button" aria-label="关闭" :disabled="loading" @click="close">
          <svg viewBox="0 0 24 24" width="16" height="16">
            <path
              fill="currentColor"
              d="M18.3 5.71 12 12l6.3 6.29-1.41 1.42L10.59 13.4 4.3 19.71 2.89 18.3 9.17 12 2.89 5.71 4.3 4.29l6.29 6.3 6.3-6.3z"
            />
          </svg>
        </button>

        <div class="modal-body">
          <span class="modal-icon" :class="`modal-icon--${type}`" aria-hidden="true">
            <svg v-if="type === 'info'" viewBox="0 0 24 24" width="28" height="28">
              <path fill="currentColor" d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20m1 15h-2v-6h2zm0-8h-2V7h2z" />
            </svg>
            <svg v-else viewBox="0 0 24 24" width="28" height="28">
              <path fill="currentColor" d="M1 21h22L12 2zm12-3h-2v-2h2zm0-4h-2v-4h2z" />
            </svg>
          </span>
          <h3 class="modal-title">{{ title }}</h3>
          <p class="modal-message">{{ message }}</p>
        </div>

        <div class="modal-footer">
          <button
            v-if="showCancel"
            class="modal-btn modal-btn--cancel"
            type="button"
            :disabled="loading"
            @click="close"
          >
            {{ cancelText }}
          </button>
          <button
            class="modal-btn"
            :class="`modal-btn--${type}`"
            type="button"
            :disabled="loading"
            @click="confirm"
          >
            <span v-if="loading" class="modal-btn-spinner" aria-hidden="true"></span>
            {{ confirmText }}
          </button>
        </div>
      </div>
      </div>
    </transition>
  </Teleport>
</template>

<style scoped>
.modal-root {
  position: fixed;
  inset: 0;
  z-index: 100001;
  display: flex;
  align-items: center;
  justify-content: center;
}

.modal-overlay {
  position: absolute;
  inset: 0;
  /* 全局 styles/index.css 的 .modal-overlay 带 z-50，会把遮罩盖到弹窗按钮上方，
     导致「取消 / 确定」点不动。此处显式降为 0，并让弹窗主体抬到 1。 */
  z-index: 0;
  background: rgba(0, 0, 0, 0.5);
  /* 鼠标在遮罩上显示默认箭头，提示不可点击关闭 */
  cursor: default;
}

.modal-dialog {
  position: relative;
  z-index: 1;
  width: 100%;
  max-width: 380px;
  margin: 0 16px;
  background: var(--color-bg, #ffffff);
  color: var(--color-text, #1f2329);
  border: 1px solid var(--color-border, #e5e6eb);
  border-radius: 12px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.2);
}

.modal-x {
  position: absolute;
  top: 12px;
  right: 12px;
  display: inline-flex;
  border: none;
  background: transparent;
  color: #8c8c8c;
  cursor: pointer;
  padding: 4px;
  border-radius: 6px;
}

.modal-x:hover:not(:disabled) {
  background: rgba(0, 0, 0, 0.06);
  color: var(--color-text, #1f2329);
}

.modal-x:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.modal-body {
  padding: 28px 24px 12px;
  text-align: center;
}

.modal-icon {
  display: inline-flex;
  margin-bottom: 12px;
}

.modal-icon--warning {
  color: #faad14;
}

.modal-icon--danger {
  color: #ff4d4f;
}

.modal-icon--info {
  color: #1677ff;
}

.modal-title {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 8px;
}

.modal-message {
  font-size: 14px;
  color: var(--color-text, #1f2329);
  opacity: 0.85;
  word-break: break-word;
}

.modal-footer {
  display: flex;
  gap: 12px;
  padding: 12px 24px 24px;
}

.modal-btn {
  flex: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 9px 16px;
  border: none;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 0.15s ease;
}

.modal-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.modal-btn--cancel {
  background: #f2f3f5;
  color: #1f2329;
}

.modal-btn--cancel:hover:not(:disabled) {
  background: #e5e6eb;
}

.modal-btn--warning {
  background: #faad14;
  color: #ffffff;
}

.modal-btn--warning:hover:not(:disabled) {
  background: #d48806;
}

.modal-btn--danger {
  background: #ff4d4f;
  color: #ffffff;
}

.modal-btn--danger:hover:not(:disabled) {
  background: #d9363e;
}

.modal-btn--info {
  background: #1677ff;
  color: #ffffff;
}

.modal-btn--info:hover:not(:disabled) {
  background: #0958d9;
}

.modal-btn-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255, 255, 255, 0.4);
  border-top-color: #ffffff;
  border-radius: 50%;
  animation: modal-rotate 0.8s linear infinite;
}

@keyframes modal-rotate {
  to {
    transform: rotate(360deg);
  }
}

.modal-fade-enter-active,
.modal-fade-leave-active {
  transition: opacity 0.2s ease;
}

.modal-fade-enter-from,
.modal-fade-leave-to {
  opacity: 0;
}
</style>
