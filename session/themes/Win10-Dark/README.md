# Win10 Dark for Openbox 3

把 Win10 深色模式的纯黑标题栏、左对齐标题、白色线条按钮和深灰菜单做成 Openbox 主题。非活动标题栏用 #181818，关闭按钮悬停用 #E81123。

## 安装与预览参数

把 Win10-Dark 目录放入 ~/.themes 或 /usr/share/themes，再在 Openbox 的 rc.xml 中将 theme/name 设为 Win10-Dark。已有会话可用 openbox --reconfigure 重新加载。

效果图使用 NLIMC 标题栏布局。ActiveWindow 和 InactiveWindow 使用 Liberation Sans Regular 10 pt。菜单和屏幕提示使用 Noto Sans CJK SC Regular 10 pt。字体在 rc.xml 的 theme/font 中设置。

Openbox 3.6.1 将悬停底色画在标题栏的方形按钮内。窗口图标、工具栏和文件列表由 Wine 应用提供，效果图使用 PortOS 会话镜像原有的 Wine 配色。

已在一次性容器中用 Openbox 3.6.1、TigerVNC 和 Wine 10.19 验证。PortOS 的中文窗口菜单来自其现有配置。
