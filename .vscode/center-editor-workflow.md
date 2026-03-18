# Center editor workflow for VS Code / Cursor

This workspace is configured to work best with a three-column editor layout where the center group is your primary editing pane.

## One-time setup
1. Run `View: Editor Layout: Three Columns`.
2. Open your reference files in the left and right groups.
3. Keep your main working file open in the center group.
4. Optionally lock the left and right groups with `View: Lock Editor Group`.

## Daily workflow
1. Run `View: Focus Second Editor Group`.
2. Open files from the Explorer, Search view, or `Cmd+P` / `Ctrl+P`.
3. Files will open in the center group while it has focus.

## Optional keybinding
VS Code and Cursor do not support workspace-specific keybindings, so add this to your user keybindings if you want a shortcut for focusing the center group:

```json
{
  "key": "cmd+alt+2",
  "command": "workbench.action.focusSecondEditorGroup"
}
```

Use `ctrl+alt+2` instead if you prefer a Windows/Linux shortcut.
