
import numpy as np
import paddle
import re

from .abc_interpreter import Interpreter
from ..data_processor.readers import preprocess_inputs, preprocess_save_path
from ..data_processor.visualizer import explanation_to_vis, show_vis_explanation, save_image


class TAMInterpreter(Interpreter):
    """
    Transition Attention Maps Interpreter.

    More details regarding the Transition_Attention_Maps method can be found in the original paper:
    Tingyi Yuan, Xuhong Li, Haoyi Xiong, Hui Cao, Dejing Dou.
    Explaining Information Flow Inside Vision Transformers Using Markov Chain.
    """

    def __init__(self,
                 paddle_model,
                 use_cuda=True,
                 model_input_shape=[3, 224, 224]) -> None:
        """
        Initialize the TAMInterpreter.

        Args:
            paddle_model (callable): A paddle model that outputs predictions.
            use_cuda (bool, optional): Whether or not to use cuda. Default: True
            model_input_shape (list, optional): The input shape of the model. Default: [3, 224, 224]
        """
        Interpreter.__init__(self)
        self.paddle_model = paddle_model
        self.model_input_shape = model_input_shape
        self.paddle_prepared = False

        self.use_cuda = use_cuda
        if not paddle.is_compiled_with_cuda():
            self.use_cuda = False

        # init for usages during the interpretation.
        self.attns = []
        self.hooks = []

    def interpret(self,
                  inputs,
                  start_layer=4,
                  label=None,
                  visual=True,
                  save_path=None):
        """
        Main function of the interpreter.

        Args:
            inputs (str or list of strs or numpy.ndarray): The input image filepath or a list of filepaths or numpy array of read images.
            labels (list or tuple or numpy.ndarray, optional): The target labels to analyze. The number of labels should be equal to the number of images. If None, the most likely label for each image will be used. Default: None
            visual (bool, optional): Whether or not to visualize the processed image. Default: True
            save_path (str or list of strs or None, optional): The filepath(s) to save the processed image(s). If None, the image will not be saved. Default: None

        :return: interpretations/heatmap for each image
        :rtype: numpy.ndarray
        """

        imgs, data = preprocess_inputs(inputs, self.model_input_shape)
        bsz = len(data)  # batch size
        save_path = preprocess_save_path(save_path, bsz)

        if not self.paddle_prepared:
            self._paddle_prepare()

        output, preds = self.predict_fn(data)
        assert start_layer < len(self.attns), "start_layer should be in the range of [0, num_block-1]"

        if label == None:
            label = np.argmax(output.numpy(), axis=-1)

        b, h, s, _ = self.attns[0].shape
        num_blocks = len(self.attns)
        states = self.attns[-1].detach().mean(1)[:, 0, :].reshape((b, 1, s))
        for i in range(start_layer, num_blocks-1)[::-1]:
            attn = self.attns[i].detach().mean(1)
            states_ = states
            states = states @ attn
            states += states_

        total_gradients = paddle.zeros((b, h, s, s))
        steps = 20
        for alpha in np.linspace(0, 1, steps):
            # forward propagation
            data_scaled = data * alpha
            output, preds = self.predict_fn(data_scaled)
            # backward propagation
            one_hot = np.zeros((b, output.shape[-1]), dtype=np.float32)
            one_hot[np.arange(b), label] = 1
            one_hot = paddle.to_tensor(one_hot, stop_gradient=False)
            target = paddle.sum(one_hot * output)

            target.backward()
            gradients = self.attns[-1].grad
            target.clear_gradient()

            gradients = paddle.to_tensor(gradients)
            total_gradients += gradients.detach()

        W_state = (total_gradients / steps).clip(min=0).mean(1)[:, 0, :].reshape((b, 1, s)).detach()

        tam_explanation = (states * W_state)[:, 0, 1:].reshape((-1, 14, 14)).cpu().detach().numpy()

        # visualization and save image.
        for i in range(bsz):
            vis_explanation = explanation_to_vis(imgs[i], tam_explanation[i], style='overlay_heatmap')
            if visual:
                show_vis_explanation(vis_explanation)
            if save_path[i] is not None:
                save_image(save_path[i], vis_explanation)

        return tam_explanation

    def _paddle_prepare(self, predict_fn=None):
        if predict_fn is None:
            paddle.set_device('gpu:0' if self.use_cuda else 'cpu')
            # to get gradients, the ``train`` mode must be set.
            # we cannot set v.training = False for the same reason.
            self.paddle_model.train()

            def hook(layer, input, output):
                self.attns.append(output)

            self.hooks = []
            for n, v in self.paddle_model.named_sublayers():
                if "batchnorm" in v.__class__.__name__.lower():
                    v._use_global_stats = True
                if "dropout" in v.__class__.__name__.lower():
                    v.p = 0

                # Report issues or pull requests if more layers need to be added.

            def predict_fn(data):
                data = paddle.to_tensor(data)
                data.stop_gradient = False
                self.attns = []
                self.hooks = []
                for n, v in self.paddle_model.named_sublayers():
                    if re.match('^blocks.*.attn.attn_drop$', n):
                        h = v.register_forward_post_hook(hook)
                        self.hooks.append(h)
                output = self.paddle_model(data)
                for h in self.hooks:
                    h.remove()

                out = paddle.nn.functional.softmax(output, axis=1)
                preds = paddle.argmax(out, axis=1)
                label = preds.numpy()

                return output, label

        self.predict_fn = predict_fn
        self.paddle_prepared = True
