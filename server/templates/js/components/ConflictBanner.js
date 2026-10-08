/* ConflictBanner 组件：agent 写入冲突提示 */
import { ICONS } from '../utils/icons.js'

export const ConflictBanner = {
  name: 'ConflictBanner',
  props: {
    toolName: { type: String, default: 'write_doc' },
    path: { type: String, default: '' }
  },
  emits: ['accept', 'dismiss'],
  setup(props, { emit }) {
    const icon = ICONS.alert

    function accept() {
      emit('accept')
    }

    function dismiss() {
      emit('dismiss')
    }

    return { icon, accept, dismiss }
  },
  template: `
    <div class="conflict-banner">
      <span class="conflict-msg">
        <span v-html="icon"></span>
        {{ toolName === 'write_doc' ? 'agent 已写入此文档' : 'agent 已编辑此文档' }}，你的未保存编辑将被覆盖
      </span>
      <button class="conflict-keep" @click="dismiss">保留我的</button>
      <button class="conflict-accept" @click="accept">接受覆盖</button>
    </div>
  `
}
