"""Audio or Silence: pass a video's audio through, or emit silence of matching length when there is none.

LTX Refine's graph encodes audio alongside the video; plates read through OCIO Read have no audio track, so
this keeps VAEEncodeAudio from receiving None. Duration = frame count / fps of the decoded video.
"""
import torch


class AudioOrSilence:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE", {"tooltip": "Decoded video frames; their count sets the silence length."}),
                "fps": ("FLOAT", {"default": 24.0, "min": 0.01, "max": 1000.0, "step": 0.01, "forceInput": True}),
            },
            "optional": {
                "audio": ("AUDIO", {"tooltip": "Source audio. May be empty/None when the video has no track."}),
            },
        }

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("audio",)
    FUNCTION = "run"
    CATEGORY = "audio/utils"
    DESCRIPTION = "Returns the input audio if present, otherwise silence as long as the video."

    def run(self, images, fps, audio=None):
        if audio is not None and audio.get("waveform") is not None and audio["waveform"].numel() > 0:
            return (audio,)
        sample_rate = 44100
        n = max(1, int(round(images.shape[0] / float(fps) * sample_rate)))
        return ({"waveform": torch.zeros(1, 2, n), "sample_rate": sample_rate},)


NODE_CLASS_MAPPINGS = {"AudioOrSilence": AudioOrSilence}
NODE_DISPLAY_NAME_MAPPINGS = {"AudioOrSilence": "Audio or Silence"}
