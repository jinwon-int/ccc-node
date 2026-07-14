.[0] as $base
| .[1] as $overlay
| ($base.hooks // {}) as $base_hooks
| ($overlay.hooks // {}) as $overlay_hooks
| if (($base_hooks | type) != "object" or ($overlay_hooks | type) != "object") then
    error("settings hooks must be objects")
  elif (([$base_hooks[], $overlay_hooks[]] | all(type == "array")) | not) then
    error("each settings hook event must be an array")
  else
    $base
    | .hooks = (
        reduce ($overlay_hooks | to_entries[]) as $event
          ($base_hooks; .[$event.key] = ((.[$event.key] // []) + $event.value))
      )
  end
