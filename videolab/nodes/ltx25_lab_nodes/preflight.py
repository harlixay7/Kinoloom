"""Lab-only: no-op OUTPUT_NODE used by conditioning-preflight graphs.

ComfyUI refuses prompts with no output node (execution.py: "Prompt has no
outputs"). A preflight graph needs only CLIPLoader + CLIPTextEncode nodes to
populate the execution cache for later renders, so this node exists purely to
satisfy that requirement without writing any file.

First-party lab node, shipped in videolab/nodes/ltx25_lab_nodes.
"""


class LTX25LabPreflight:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("*",)}}

    RETURN_TYPES = ()
    FUNCTION = "sink"
    CATEGORY = "LTX25_LAB"
    OUTPUT_NODE = True

    def sink(self, value):
        print("[LTX25 lab] preflight sink reached", flush=True)
        return {}


NODE_CLASS_MAPPINGS = {"LTX25LabPreflight": LTX25LabPreflight}
NODE_DISPLAY_NAME_MAPPINGS = {"LTX25LabPreflight": "LTX25 Lab Preflight Sink"}
