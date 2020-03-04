import warnings

import torch
import torch.nn as nn
import torch.nn.functional as F
from decorator import contextmanager

from IBA.pytorch import IBA, TorchWelfordEstimator, _InterruptExecution


class IBAReadout(IBA):
    """
    The Readout Bottleneck is an extension to yield the alphas for the IBA bottleneck
    from a readout network. The readout network is trained on intermediate feature maps
    which are obtained by performing a nested forward pass on the model and recording
    activations.
    Major differences to the Per-Sample IBA:
    - an additional context manager for the nested pass
    - additional hooks to collect the input and the feature maps in the nested pass
    - a readout network of three 1x1 conv. layers to yield alpha
    """
    def __init__(self, attach_layer, readout_layers, model, **kwargs):
        super().__init__(attach_layer, **kwargs)
        self.layers = readout_layers
        self.model = model  # used for the nested pass
        self._readout_estimators = [TorchWelfordEstimator() for _ in readout_layers]
        self._readout_values = [None for _ in readout_layers]  # The recorded intermediate activations
        self._readout_hooks = [None for _ in readout_layers]  # Registered hooks
        self._input_hook = None  # To record the input
        self._last_input = None  # Used as input for the nested forward pass
        self._nested_pass = False

        # Attach additional hooks to capture input and readout
        self._attach_input_hook()
        self._attach_readout_hooks()

    def _init(self):
        super()._init()
        # Use the estimators to get feature map dimensions
        features_in = sum(map(lambda e: e.shape[0], self._readout_estimators))
        features_out = self.estimator.shape[-3]
        # Define readout network layers
        self.relu = nn.ReLU(inplace=False)
        self.conv1 = nn.Conv2d(in_channels=features_in, out_channels=features_in//2, kernel_size=1)
        self.conv2 = nn.Conv2d(in_channels=features_in//2, out_channels=features_out*2, kernel_size=1)
        self.conv3 = nn.Conv2d(in_channels=features_out*2, out_channels=features_out, kernel_size=1)
        # Initialize with identity mapping
        with torch.no_grad():
            nn.init.constant_(self.conv3.bias, 5.0)
            self.conv3.weight *= 1e-3
        # Put weights on same device
        self.to(self.estimator.device)

    def detach(self):
        super().detach()
        if self._input_hook:
            # Remove input hook
            self._input_hook.detach()
            self._input_hook = None
            # Remove readout hooks
            [h.detach() for h in self._readout_hooks]
            self._readout_hooks = [None for _ in self._readout_hooks]

    def heatmap(self, input_t, model, **kwargs):
        if len(kwargs) > 0:
            warnings.warn(f"Additional arguments ({list(kwargs.keys())}) are ignored in the Readout IBA.")
        # Pass the input through the model
        with self.supress_information(), torch.no_grad():
            model(input_t)
        # Read heatmap
        return self._current_heatmap(input_t.shape[2:])

    def _attach_readout_hooks(self):
        """
        Attach a hook to every readout layer. They feed the feature maps to the
        estimators for mean and variance. In the nested pass, they are recorded
        to use them in the readout network.
        """
        for i, layer in enumerate(self.layers):
            # Create hook closure for this layer
            def create_read_hook(j):
                def read_hook(module, inputs, outputs):
                    if self._nested_pass:
                        # Record for second forward pass with activated bottleneck
                        self._readout_values[j] = outputs.clone().detach()
                    elif self._estimate:
                        # Estimate mean and std
                        self._readout_estimators[j](outputs)
                    else:
                        # Hook is neutral when not used (just as the Per-Sample IBA)
                        pass
                return read_hook
            # Attach the hook
            self._readout_hooks[i] = layer.register_forward_hook(create_read_hook(i))

    def _attach_input_hook(self):
        """
        Attach a pre-hook to the model to capture the input. It will be used
        again as model input for the nested pass to obtain the feature maps.
        """
        def input_hook(module, inputs):
            if self._supress_information and not self._nested_pass:
                self._last_input = inputs[0].clone()
        self._input_hook = self.model.register_forward_pre_hook(input_hook)

    def reset_estimate(self):
        """ Additionaly reset estimators of bottleneck layers. """
        super().reset_estimate()
        self._readout_estimators = [TorchWelfordEstimator() for _ in self.layers]

    def forward(self, x):
        if self._supress_information:
            if not self._nested_pass:
                # Obtain alpha using the readout and readout network
                alpha = self._generate_alpha()
                # Suppress information identically to the Per-Sample IBA
                return self._do_restrict_information(x, alpha)
        if self._estimate:
            self.estimator(x)
        if self._interrupt_execution:
            # We don't interrupt execution in the Readout Bottleneck, as we
            # need to read out feature maps of later layers for the readout network
            pass
        return x

    def _generate_alpha(self):
        """
        Run a nested forward pass on the model with the same input, then use the
        recorded feature maps in a 3-layer Readout Network to generate alpha.
        """
        # Run a nested pass to obtain feature maps, stored in self._readout_values
        with self.nested_pass():
            self.model(self._last_input)

        # Normalize using the estimators
        readouts = [(r - e.mean()) / e.std() for r, e in zip(self._readout_values, self._readout_estimators)]

        # Resize to fit shape of bottleneck layer
        spatial_shape = self.estimator.shape[-2:]
        # Expand readouts of fully connected layers to feature maps
        readouts = [r[..., None, None].expand(*r.shape, *spatial_shape) if len(r.shape) == 2 else r for r in readouts]
        # Interpolate to get identical spatial shape as x
        readouts = [F.interpolate(input=r, size=spatial_shape, mode="bilinear", align_corners=True) for r in readouts]

        # Stack normalized readout values
        readout = torch.cat(readouts, dim=1)

        # Pass through the readout network to obtain alpha
        alpha = self.conv1(readout)
        alpha = self.relu(alpha)
        alpha = self.conv2(alpha)
        alpha = self.relu(alpha)
        alpha = self.conv3(alpha)

        return alpha

    @contextmanager
    def nested_pass(self):
        """
        Context manager to pass the input once though the model in a nested pass to
        obtain the readout feature maps. These are then used as the input for the readout
        network to predict alphas for the bottleneck.
        """
        self._nested_pass = True
        try:
            yield
        finally:
            self._nested_pass = False
