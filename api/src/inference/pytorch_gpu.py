"""GPU-based PyTorch inference backend."""

import gc
from typing import Optional

import numpy as np
import torch
from loguru import logger

from ..builds.models import build_model
from ..core import paths
from ..core.model_config import model_config
from .base import BaseModelBackend


@torch.no_grad()
def forward(model: torch.nn.Module, tokens: list[int], ref_s: torch.Tensor, speed: float) -> np.ndarray:
    """Forward pass through model.
    
    Args:
        model: PyTorch model
        tokens: Input tokens
        ref_s: Reference signal (shape: [1, n_features])
        speed: Speed multiplier
        
    Returns:
        Generated audio
    """
    device = ref_s.device
    
    # Initial tensor setup
    tokens = torch.LongTensor([[0, *tokens, 0]]).to(device)
    input_lengths = torch.LongTensor([tokens.shape[-1]]).to(device)
    text_mask = length_to_mask(input_lengths).to(device)

    # Split reference signals (style_dim=128 from config)
    style_dim = 128
    s_ref = ref_s[:, :style_dim].clone().to(device)
    s_content = ref_s[:, style_dim:].clone().to(device)

    # BERT and encoder pass
    bert_dur = model.bert(tokens, attention_mask=(~text_mask).int())
    d_en = model.bert_encoder(bert_dur).transpose(-1, -2)

    # Predictor forward pass
    d = model.predictor.text_encoder(d_en, s_content, input_lengths, text_mask)
    x, _ = model.predictor.lstm(d)

    # Duration prediction
    duration = model.predictor.duration_proj(x)
    duration = torch.sigmoid(duration).sum(axis=-1) / speed
    pred_dur = torch.round(duration).clamp(min=1).long()
    del duration, x

    # Alignment matrix construction
    pred_aln_trg = torch.zeros(input_lengths.item(), pred_dur.sum().item(), device=device)
    c_frame = 0
    for i in range(pred_aln_trg.size(0)):
        pred_aln_trg[i, c_frame:c_frame + pred_dur[0, i].item()] = 1
        c_frame += pred_dur[0, i].item()
    pred_aln_trg = pred_aln_trg.unsqueeze(0)

    # Matrix multiplications
    en = d.transpose(-1, -2) @ pred_aln_trg
    del d
    
    F0_pred, N_pred = model.predictor.F0Ntrain(en, s_content)
    del en

    # Final text encoding and decoding
    t_en = model.text_encoder(tokens, input_lengths, text_mask)
    asr = t_en @ pred_aln_trg
    del t_en

    # Generate output
    output = model.decoder(asr, F0_pred, N_pred, s_ref)
    return output.squeeze().cpu().numpy()


def length_to_mask(lengths: torch.Tensor) -> torch.Tensor:
    """Create attention mask from lengths."""
    max_len = lengths.max()
    mask = torch.arange(max_len, device=lengths.device)[None, :].expand(lengths.shape[0], -1)
    if lengths.dtype != mask.dtype:
        mask = mask.to(dtype=lengths.dtype)
    return mask + 1 > lengths[:, None]


class PyTorchGPUBackend(BaseModelBackend):
    """PyTorch GPU inference backend."""

    def __init__(self):
        """Initialize GPU backend."""
        super().__init__()
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA not available")
        self._device = "cuda"
        self._model: Optional[torch.nn.Module] = None
        
        # Configure GPU settings
        config = model_config.pytorch_gpu
        if config.sync_cuda:
            torch.cuda.synchronize()
        torch.cuda.set_device(config.device_id)

    async def load_model(self, path: str) -> None:
        """Load PyTorch model.
        
        Args:
            path: Path to model file
            
        Raises:
            RuntimeError: If model loading fails
        """
        try:
            # Get verified model path
            model_path = await paths.get_model_path(path)
            
            logger.info(f"Loading PyTorch model: {model_path}")
            self._model = await build_model(model_path, self._device)
            
        except Exception as e:
            raise RuntimeError(f"Failed to load PyTorch model: {e}")

    def generate(
        self,
        tokens: list[int],
        voice: torch.Tensor,
        speed: float = 1.0
    ) -> np.ndarray:
        """Generate audio using GPU model.
        
        Args:
            tokens: Input token IDs
            voice: Voice embedding tensor
            speed: Speed multiplier
            
        Returns:
            Generated audio samples
            
        Raises:
            RuntimeError: If generation fails
        """
        if not self.is_loaded:
            raise RuntimeError("Model not loaded")

        try:
            # Check memory and cleanup if needed
            if self._check_memory():
                self._clear_memory()
                
            # Get reference style from voice pack
            ref_s = voice[len(tokens)].clone().to(self._device)
            if ref_s.dim() == 1:
                ref_s = ref_s.unsqueeze(0)  # Add batch dimension if needed
            
            # Generate audio
            return forward(self._model, tokens, ref_s, speed)
            
        except Exception as e:
            logger.error(f"Generation failed: {e}")
            if model_config.pytorch_gpu.retry_on_oom and "out of memory" in str(e).lower():
                self._clear_memory()
                return self.generate(tokens, voice, speed)  # Retry once
            raise
        finally:
            if model_config.pytorch_gpu.sync_cuda:
                torch.cuda.synchronize()

    def _check_memory(self) -> bool:
        """Check if memory usage is above threshold."""
        if torch.cuda.is_available():
            memory_gb = torch.cuda.memory_allocated() / 1e9
            return memory_gb > model_config.pytorch_gpu.memory_threshold
        return False

    def _clear_memory(self) -> None:
        """Clear GPU memory."""
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            gc.collect()