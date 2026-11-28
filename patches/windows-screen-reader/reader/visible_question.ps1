param([long]$Foreground = 0, [long]$Preferred = 0, [string]$PreviousTarget)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class QuestionPointer {
    [StructLayout(LayoutKind.Sequential)] public struct Point { public int X, Y; }
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out Point point);
    [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(Point point);
    [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr window, uint flag);
    [DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
}
'@
$null = [QuestionPointer]::SetThreadDpiAwarenessContext([IntPtr](-4))
$initialPointer = [QuestionPointer+Point]::new()
$hasInitialPointer = [QuestionPointer]::GetCursorPos([ref]$initialPointer)
$A = [Windows.Automation.AutomationElement]
$T = [Windows.Automation.TreeScope]
$P = [Windows.Automation.PropertyCondition]
$browserIds = @(Get-Process msedge,chrome -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
$windows = @($A::RootElement.FindAll($T::Children,[Windows.Automation.Condition]::TrueCondition) | Where-Object {
    $_.Current.NativeWindowHandle -ne 0 -and $_.Current.ProcessId -in $browserIds
})
$focused = @($windows | Where-Object {$_.Current.NativeWindowHandle -eq $Foreground})
$last = @($windows | Where-Object {$_.Current.NativeWindowHandle -eq $Preferred -and $_.Current.NativeWindowHandle -ne $Foreground})
$other = @($windows | Where-Object {$_.Current.NativeWindowHandle -ne $Foreground -and $_.Current.NativeWindowHandle -ne $Preferred})
$windows = $focused + $last + $other
foreach ($root in $windows) {
    $doc = $root.FindFirst($T::Descendants,$P::new($A::ControlTypeProperty,[Windows.Automation.ControlType]::Document))
    if (!$doc) { continue }
    $bounds = $doc.Current.BoundingRectangle
    $cache = [Windows.Automation.CacheRequest]::new()
    $cache.Add($A::NameProperty)
    $cache.Add($A::ControlTypeProperty)
    $cache.Add($A::BoundingRectangleProperty)
    $active = $cache.Activate()
    try {
        $filter = [Windows.Automation.OrCondition]::new($P::new($A::ControlTypeProperty,[Windows.Automation.ControlType]::Text),$P::new($A::ControlTypeProperty,[Windows.Automation.ControlType]::Image))
        $links = $P::new($A::ControlTypeProperty,[Windows.Automation.ControlType]::Hyperlink)
        $controls = $P::new($A::ControlTypeProperty,[Windows.Automation.ControlType]::Edit)
        $nodes = $doc.FindAll($T::Descendants,[Windows.Automation.OrCondition]::new([Windows.Automation.OrCondition]::new($filter,$links),$controls))
    } finally { $active.Dispose() }
    $top = $bounds.Top
    $bottom = $bounds.Bottom
    $groups = @()
    $group = $null
    $editorGroup = $null
    foreach ($node in $nodes) {
        $name = $node.Cached.Name.Trim()
        $rect = $node.Cached.BoundingRectangle
        if ($node.Cached.ControlType -eq [Windows.Automation.ControlType]::Edit) {
            $owner = if ($group) { $group } else { $editorGroup }
            if ($owner -and !$rect.IsEmpty) { $owner.end = [Math]::Max($owner.end,$rect.Bottom) }
            if ($group -and $group.kind -eq '填空题') { $group.blankEditors++ }
            continue
        }
        if ($group -and $group.kind -eq '简答题' -and $name -match '^(?:[*＊]?本题暂不支持系统判断|请输入(?:你的|您的)?答案$|单题模式$)') {
            $group.imageBottom = $rect.Top
            $group.end = [Math]::Max($group.end,$rect.Top)
            $editorGroup = $group
            $group = $null
            continue
        }
        if ($group -and $name -match '^(添加笔记|邀请回答|添加回答|求解答(?:[（(]\d+[)）])?|查看答案(?:及解析)?|参考答案|答案及解析|全部回答|\d+\s*个回答)[：:]?$') {
            $group = $null
            continue
        }
        if ($node.Cached.ControlType -eq [Windows.Automation.ControlType]::Hyperlink) { continue }
        $label = $name.Trim([char[]]'[]【】［］').Trim()
        if ($name -eq '退出答题') { $top = [Math]::Max($top,$rect.Bottom); continue }
        if ($name -eq '答题卡' -and $rect.Top -gt $top) { $bottom = [Math]::Min($bottom,$rect.Top); break }
        if ((!$group -and $name -match '^(题目描述|问题描述|Problem Statement)[：:]?$') -or $label -match '^(单选题|单项选择题|多选题|多项选择题|不定项选择题|判断题|填空题|问答题|简答题|论述题|材料分析题|编程题|算法题|Programming Problem)$') {
            $kind = switch -Regex ($label) {
                '^(题目描述|问题描述|Problem Statement|Programming Problem|算法题)' {'编程题';break}
                '^单项选择题$' {'单选题';break}
                '^多项选择题$' {'多选题';break}
                '^(简答题|问答题|论述题|材料分析题)$' {'简答题';break}
                default {$label}
            }
            $group = @{kind=$kind;y=$rect.Top;end=$rect.Bottom;heading=$node;lines=[Collections.Generic.List[object]]::new()}
            $editorGroup = $null
            $groups += $group
        } elseif ($group -and $name -match '^(正确答案|参考答案|你的答案|我的答案|官方解析|答案解析)[：:]') {
            $group = $null
        } elseif ($group -and $group.kind -eq '编程题' -and $name -eq 'ACM 模式') {
            $group = $null
        } elseif ($group -and $name -and $name -notmatch '^(收藏|反馈|更多)$') {
            $fragment = $node.Cached.Name
            if ($group.kind -eq '填空题') {
                if ($group.inAnswerPanel) { continue }
                if ($name -match '^[1-9]\d*$') {
                    $container = [Windows.Automation.TreeWalker]::RawViewWalker.GetParent($node)
                    $classes = $container.Current.ClassName -split '\s+'
                    if ($classes -contains 'fillBlankClass') {
                        $fragment = "【空$name】"
                    } elseif ($classes -contains 'label') {
                        $group.inAnswerPanel = $true
                        continue
                    }
                }
            }
            $loc = $null
            if (!$rect.IsEmpty -and $rect.Height -gt 0) {
                $loc = @($rect.Left,$rect.Top,$rect.Width,$rect.Height)
            }
            if ($loc -and $group.kind -eq '简答题' -and $name -match '^\d+[.．、]$') {
                $group.nodeID = $node.GetRuntimeId() -join '.'
                $parent = $node
                $walker = [Windows.Automation.TreeWalker]::RawViewWalker
                while ($parent -and $parent -ne $doc) {
                    $parent = $walker.GetParent($parent)
                    if (!$parent) { break }
                    $area = $parent.Current.BoundingRectangle
                    if ($area.Width -gt 160 -and $area.Height -gt 80) {
                        $group.imageRight = $area.Right
                        break
                    }
                }
            }
            $group.lines.Add(@{text=$fragment; rect=$loc})
            $group.end = [Math]::Max($group.end,$rect.Bottom)
        }
    }
    $targets = @($groups | Where-Object { $_.lines.Count -and $_.lines[0].text.Trim() -notmatch '^\d+$' })
    $edit = $doc.FindFirst($T::Descendants,$P::new($A::HasKeyboardFocusProperty,$true))
    $walker = [Windows.Automation.TreeWalker]::RawViewWalker
    $keyboard = $A::FocusedElement
    if ($keyboard -and $keyboard.Current.ProcessId -eq $root.Current.ProcessId) {
        # Chromium remembers the last focused field even when the address bar is active.
        $edit = $null
        $answerControl = $null
        for ($hostNode=$keyboard; $hostNode; $hostNode=$walker.GetParent($hostNode)) {
            if ($hostNode -eq $doc) { $edit=$answerControl; break }
            if ($hostNode -eq $root) { break }
            if ($hostNode.Current.ControlType.ProgrammaticName -in @('ControlType.Edit','ControlType.RadioButton','ControlType.CheckBox','ControlType.ComboBox')) {
                $answerControl=$hostNode
            }
        }
    }
    $focusRect = $null
    if ($edit -and $edit.Current.ControlType.ProgrammaticName -in @('ControlType.Edit','ControlType.RadioButton','ControlType.CheckBox','ControlType.ComboBox')) {
        $focusRect = $edit.Current.BoundingRectangle
    }
    $mouse = [QuestionPointer+Point]::new()
    $overBrowser = [QuestionPointer]::GetCursorPos([ref]$mouse) -and
        [QuestionPointer]::GetAncestor([QuestionPointer]::WindowFromPoint($mouse),2).ToInt64() -eq $root.Current.NativeWindowHandle
    $readingLine = $top + ($bottom-$top)*.18
    $best = $null
    $bestRank = -1
    $bestDistance = [double]::PositiveInfinity
    for ($i=0; $i -lt $targets.Count; $i++) {
        $g = $targets[$i]
        $end = if ($i+1 -lt $targets.Count -and $targets[$i+1].y -gt $g.y) { $targets[$i+1].y } else { $g.end }
        $start = [Math]::Max($top,$g.y)
        $finish = [Math]::Min($bottom,$end)
        if ($finish -le $start) { continue }
        $area = $bounds
        $parent = $walker.GetParent($g.heading)
        while ($parent -and $parent -ne $doc) {
            $rect = $parent.Current.BoundingRectangle
            if (!$rect.IsEmpty -and $rect.Width -gt 160) { $area=$rect; break }
            $parent = $walker.GetParent($parent)
        }
        $rank = 0
        $reason = 'viewport'
        $targetID = $g.heading.GetRuntimeId() -join '.'
        $wasSelected = $root.Current.NativeWindowHandle -eq $Preferred -and $targetID -eq $PreviousTarget
        if ($overBrowser -and $mouse.X -ge $area.Left -and $mouse.X -lt $area.Right -and $mouse.Y -ge $start -and $mouse.Y -lt $finish) {
            if ($wasSelected -or ($hasInitialPointer -and $initialPointer.X -ge $area.Left -and $initialPointer.X -lt $area.Right -and $initialPointer.Y -ge $start -and $initialPointer.Y -lt $finish)) {
                $rank = 1
                $reason = 'pointer'
            }
        }
        if ($focusRect -and !$focusRect.IsEmpty) {
            $fx = $focusRect.Left + $focusRect.Width/2
            $fy = $focusRect.Top + $focusRect.Height/2
            if ($fx -ge $area.Left -and $fx -lt $area.Right -and $fy -ge $start -and $fy -lt $finish) {
                $rank = 2
                $reason = 'focus'
            }
        }
        $distance = [Math]::Max(0,[Math]::Max($start-$readingLine,$readingLine-$finish))
        if ($wasSelected -and $rank -eq 0 -and $distance -le 12) { $distance = -1 }
        if ($rank -gt $bestRank -or ($rank -eq $bestRank -and $distance -lt $bestDistance)) {
            $best=$g; $bestRank=$rank; $bestDistance=$distance; $selectionReason=$reason; $selectedID=$targetID
        }
    }
    if ($best) {
        $next = $null
        $index = [Array]::IndexOf($groups,$best)
        if ($index -ge 0 -and $index+1 -lt $groups.Count) {
            $following = $groups[$index+1]
            $next = @{kind=$following.kind;lines=$following.lines;blankEditors=$following.blankEditors}
        }
        $url = $doc.GetCurrentPropertyValue([Windows.Automation.ValuePatternIdentifiers]::ValueProperty)
        $docID = $doc.GetRuntimeId() -join '.'
        @{kind=$best.kind;lines=$best.lines;blankEditors=$best.blankEditors;selection=$selectionReason;targetID=$selectedID;view=@($bounds.Left,$top,$bounds.Right,$bottom);imageRight=$best.imageRight;imageBottom=$best.imageBottom;window=$root.Current.NativeWindowHandle;url=[string]$url;docID=$docID;nodeID=$best.nodeID;browserStart=(Get-Process -Id $root.Current.ProcessId).StartTime.Ticks.ToString();next=$next} | ConvertTo-Json -Depth 7 -Compress
        exit
    }
}
'null'
