export type ViewKey = 'studio' | 'calibrate' | 'run' | 'presets' | 'settings'

/** 视图统一向 App 发出的导航事件契约。 */
export interface ViewEmits {
  (event: 'goto', view: ViewKey): void
}
