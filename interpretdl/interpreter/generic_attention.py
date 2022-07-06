import numpy as np
import re

from .abc_interpreter import Interpreter, InputGradientInterpreter
from ..data_processor.readers import images_transform_pipeline
from ..data_processor.visualizer import explanation_to_vis, show_vis_explanation, save_image


class GAInterpreter(InputGradientInterpreter):
    """
    Generic Attention Interpreter.

    This is a specific interpreter for Bi-Modal Transformers models. GAInterpreter computes the attention map with 
    gradients, and follows the operations that are similar to Rollout, with advanced modifications.

    This implementation is suitable for models with self-attention in each modality, like 
    `CLIP <https://arxiv.org/abs/2103.00020>`_.

    More details regarding the Generic Attention method can be found in the original paper:
    https://arxiv.org/abs/2103.15679.

    """

    def __init__(self, paddle_model: callable, device: str = 'gpu:0') -> None:
        """

        Args:
            paddle_model (callable): A model with :py:func:`forward` and possibly :py:func:`backward` functions.
            device (str): The device used for running ``paddle_model``, options: ``"cpu"``, ``"gpu:0"``, ``"gpu:1"`` 
                etc.
        """
        Interpreter.__init__(self, paddle_model, device)

    def interpret(self,
                  image_input: str or np.ndarray,
                  text: str,
                  text_tokenized: np.ndarray,
                  vis_attn_layer_pattern: str = '^visual.transformer.resblocks.*.attn.attn_map$',
                  txt_attn_layer_pattern: str = '^transformer.resblocks.*.attn.attn_map$',
                  start_layer: int = 11,
                  start_layer_text: int = 11,
                  resize_to: int = 224,
                  crop_to: int or None = None,
                  visual: bool = True,
                  save_path: str or None = None) -> tuple:
        """
        Given ``image_input`` and ``text_tokenized``, GAInterpreter first obtains all attention maps (of layers whose 
        name matches ``vis_attn_layer_pattern`` or ``txt_attn_layer_pattern`` for visual and text modalities 
        respectively) and their gradients of the prediction. Then, GAInterpreter computes the multiplication between 
        the attention map and its gradient for each block, and obtains the matrix multiplication of all blocks. The 
        ``start_layer`` controls the number of involved layers. The order of involving attention maps (from the first
        layer to the last) is the same as Rollout (from first to last). 

        Args:
            image_input (str or np.ndarray): The input image filepath or a list of filepaths or numpy array of read 
                images.
            text (str): original texts, for visualization.
            text_tokenized (np.ndarray): The tokenized text for the model's input directly.
            vis_attn_layer_pattern (str, optional): the pattern name of the layers whose features will output for 
                visual modality. Defaults to ``'^visual.transformer.resblocks.*.attn.attn_map$'``.
            txt_attn_layer_pattern (str, optional): the pattern name of the layers whose features will output for 
                text modality. Defaults to ``'^transformer.resblocks.*.attn.attn_map$'``.
            start_layer (int, optional): Compute the state from the start layer for visual modality. Defaults to 
                ``11``.
            start_layer_text (int, optional): Compute the state from the start layer for text modality. Defaults to 
                ``11``.
            resize_to (int, optional): Images will be rescaled with the shorter edge being ``resize_to``. Defaults to 
                ``224``.
            crop_to (int, optional): After resize, images will be center cropped to a square image with the size 
                ``crop_to``. If None, no crop will be performed. Defaults to ``None``.
            visual (bool, optional): Whether or not to visualize the processed image. Default: ``True``.
            save_path (str, optional): The filepath(s) to save the processed image(s). If None, the image will not be 
                saved. Default: ``None``.

        Returns:
            tuple: (text_relevance: np.ndarray, image_relevance: np.ndarray)
        """

        imgs, data = images_transform_pipeline(image_input, resize_to, crop_to)
        bsz = data.shape[0]
        assert bsz == 1, "Only support one image-text pair as input."

        self._build_predict_fn(vis_attn_layer_pattern=vis_attn_layer_pattern,
                               txt_attn_layer_pattern=txt_attn_layer_pattern)

        img_attns, txt_attns, img_attns_grads, txt_attns_grads = self.predict_fn(data, text_tokenized)

        num_tokens = img_attns[0].shape[-1]
        R = np.eye(num_tokens, num_tokens, dtype=img_attns[0].dtype)
        R = np.expand_dims(R, 0)
        R = R.repeat(bsz, axis=0)

        for i, blk in enumerate(img_attns):
            if i < start_layer:
                continue
            grad = img_attns_grads[i]
            cam = blk
            cam = cam.reshape((-1, cam.shape[-1], cam.shape[-1]))
            grad = grad.reshape((-1, grad.shape[-1], grad.shape[-1]))
            cam = grad * cam
            cam = cam.reshape((bsz, -1, cam.shape[-1], cam.shape[-1]))
            cam = cam.clip(min=0).mean(axis=1)
            R = R + np.matmul(cam, R)
        image_relevance = R[0, 0, 1:]
        image_relevance = image_relevance.reshape((7, 7))  # TODO: token's number.

        num_tokens = txt_attns[0].shape[-1]
        R_text = np.eye(num_tokens, num_tokens, dtype=img_attns[0].dtype)
        R_text = np.expand_dims(R_text, 0)
        R_text = R_text.repeat(bsz, axis=0)

        for i, blk in enumerate(txt_attns):
            if i < start_layer_text:
                continue
            grad = txt_attns_grads[i]
            cam = blk
            cam = cam.reshape((-1, cam.shape[-1], cam.shape[-1]))
            grad = grad.reshape((-1, grad.shape[-1], grad.shape[-1]))
            cam = grad * cam
            cam = cam.reshape((bsz, -1, cam.shape[-1], cam.shape[-1]))
            cam = cam.clip(min=0).mean(axis=1)
            R_text = R_text + np.matmul(cam, R_text)
        text_relevance = R_text

        # visualization and save image.
        vis_explanation = explanation_to_vis(imgs, image_relevance, style='overlay_heatmap')
        if visual:
            show_vis_explanation(vis_explanation)
            # TODO: visualizing the texts.
        if save_path is not None:
            save_image(save_path, vis_explanation)

        return text_relevance, image_relevance

    def _build_predict_fn(self, vis_attn_layer_pattern: str, txt_attn_layer_pattern: str, rebuild: bool = False):
        if self.predict_fn is not None:
            assert callable(self.predict_fn), "predict_fn is predefined before, but is not callable." \
                "Check it again."

        if self.predict_fn is None or rebuild:
            import paddle
            self._paddle_env_setup()  # inherit from InputGradientInterpreter

            def predict_fn(image, text_tokenized):
                image = paddle.to_tensor(image)
                text_tokenized = paddle.to_tensor(text_tokenized)
                #################################
                ## hooks to get attention maps ##
                img_attns = []

                def img_hook(layer, input, output):
                    img_attns.append(output)

                txt_attns = []

                def txt_hook(layer, input, output):
                    txt_attns.append(output)

                hooks = []  # for remove.
                for n, v in self.paddle_model.named_sublayers():
                    if re.match(vis_attn_layer_pattern, n):
                        h = v.register_forward_post_hook(img_hook)
                        hooks.append(h)

                    if re.match(txt_attn_layer_pattern, n):
                        h = v.register_forward_post_hook(txt_hook)
                        hooks.append(h)

                logits_per_image, logits_per_text = self.paddle_model(image, text_tokenized)

                for h in hooks:
                    h.remove()
                ## hooks to get attention maps ##
                #################################

                batch_size = text_tokenized.shape[0]
                index = [i for i in range(batch_size)]
                one_hot = np.zeros((logits_per_image.shape[0], logits_per_image.shape[1]), dtype=np.float32)
                one_hot[paddle.arange(logits_per_image.shape[0]), index] = 1
                one_hot = paddle.to_tensor(one_hot)
                one_hot = paddle.sum(one_hot * logits_per_image)
                self.paddle_model.clear_gradients()
                one_hot.backward()

                img_attns_grads = []
                for i, blk in enumerate(img_attns):
                    grad = blk.grad
                    if isinstance(grad, paddle.Tensor):
                        grad = grad.numpy()
                    img_attns_grads.append(grad)
                    img_attns[i] = blk.numpy()

                txt_attns_grads = []
                for i, blk in enumerate(txt_attns):
                    grad = blk.grad
                    if isinstance(grad, paddle.Tensor):
                        grad = grad.numpy()
                    txt_attns_grads.append(grad)
                    txt_attns[i] = blk.numpy()

                return img_attns, txt_attns, img_attns_grads, txt_attns_grads

            self.predict_fn = predict_fn

            
class GANLPInterpreter(Interpreter):
    """
    The following implementation is specially designed for Ernie.
    """

    def __init__(self, paddle_model: callable, device: str = 'gpu:0', use_cuda=None) -> None:
        """

        Args:
            paddle_model (callable): A model with :py:func:`forward` and possibly :py:func:`backward` functions.
            device (str): The device used for running ``paddle_model``, options: ``"cpu"``, ``"gpu:0"``, ``"gpu:1"`` 
                etc.
        """
        Interpreter.__init__(self, paddle_model, device, use_cuda)

    def interpret(self,
                  data: np.ndarray,
                  start_layer: int = 11,
                  label: int or None = None,
                  attn_dropout_name='^ernie.encoder.layers.*.self_attn.attn_drop$', 
                  embedding_name='^ernie.embeddings.word_embeddings$', 
                  attn_proj_name='^ernie.encoder.layers.*.self_attn.out_proj$', 
                  attn_v_name='^ernie.encoder.layers.*.self_attn.v_proj$',
                  save_path: str or None = None):
        """
        Args:
            inputs (str or list of strs or numpy.ndarray): The input image filepath or a list of filepaths or numpy 
                array of read images.
            ap_mode (str, default to head-wise): The approximation method of attentioanl perception stage, "head" for head-wise, "token" for token-wise.
            start_layer (int, optional): Compute the state from the start layer. Default: ``4``.
            steps (int, optional): number of steps in the Riemann approximation of the integral. Default: ``20``.
            labels (list or tuple or numpy.ndarray, optional): The target labels to analyze. The number of labels 
                should be equal to the number of images. If None, the most likely label for each image will be used. 
                Default: ``None``.
            resize_to (int, optional): Images will be rescaled with the shorter edge being ``resize_to``. Defaults to 
                ``224``.
            crop_to (int, optional): After resize, images will be center cropped to a square image with the size 
                ``crop_to``. If None, no crop will be performed. Defaults to ``None``.
            visual (bool, optional): Whether or not to visualize the processed image. Default: ``True``.
            save_path (str, optional): The filepath(s) to save the processed image(s). If None, the image will not be 
                saved. Default: ``None``.

        Returns:
            [numpy.ndarray]: interpretations/heatmap for images
        """

        b = data[0].shape[0]  # batch size
        assert b==1, "only support single image"
        self._build_predict_fn()
        
        attns, grads, preds = self.predict_fn(data, attn_dropout_name=attn_dropout_name,
                                            embedding_name=embedding_name,
                                            attn_proj_name=attn_proj_name,
                                            attn_v_name=attn_v_name,
                                            alpha=None)
        
        assert start_layer < len(attns), "start_layer should be in the range of [0, num_block-1]"

        if label is None:
            label = preds

        b, h, s, _ = attns[0].shape
        num_blocks = len(attns)
        R = np.eye(s, s, dtype=attns[0].dtype)
        R = np.expand_dims(R, 0)
              
        for i, blk in enumerate(attns):
            if i < start_layer-1:
                continue
            grad = grads[i]
            cam = blk
            cam = cam.reshape((b, h, cam.shape[-1], cam.shape[-1])).mean(1)
            grad = grad.reshape((b, h, grad.shape[-1], grad.shape[-1])).mean(1)
            
            cam = (cam*grad).reshape([b,s,s]).clip(min=0)

            R = R + np.matmul(cam, R)

        explanation = R[:, 0, 1:]

        return explanation

    def _build_predict_fn(self, rebuild: bool = False):
        if self.predict_fn is not None:
            assert callable(self.predict_fn), "predict_fn is predefined before, but is not callable." \
                "Check it again."

        if self.predict_fn is None or rebuild:
            import paddle
            self._paddle_env_setup()  # inherit from InputGradientInterpreter

            def predict_fn(data, attn_dropout_name=None, 
                           embedding_name=None, 
                           attn_proj_name=None, 
                           attn_v_name=None, 
                           label=None, alpha: float = 1.0):
                
                data = paddle.to_tensor(data)
                data.stop_gradient = False
                
                target_feature_map = []

                def hook(layer, input, output):
                    if alpha is not None:
                        output = alpha * output
                    target_feature_map.append(output)
                    return output

                attns = []
                def attn_hook(layer, input, output):
                    attns.append(output)
                    
                hooks = []
                for n, v in self.paddle_model.named_sublayers():
                    if re.match('^ernie.encoder.layers.*.self_attn.attn_drop$', n):
                        h = v.register_forward_post_hook(attn_hook)
                        hooks.append(h)
                    elif re.match('^ernie.embeddings.word_embeddings$', n):
                        h = v.register_forward_post_hook(hook)
                        hooks.append(h)

                out = self.paddle_model(*data)
                for h in hooks:
                    h.remove()

                out = paddle.nn.functional.softmax(out, axis=1)
                preds = paddle.argmax(out, axis=1)
                if label is None:
                    label = preds.numpy()

                attns_grads = []
                
                label_onehot = paddle.nn.functional.one_hot(paddle.to_tensor(label), num_classes=out.shape[1])
                target = paddle.sum(out * label_onehot, axis=1)
                target.backward()
                for i, blk in enumerate(attns):
                    grad = blk.grad.numpy()
                    attns_grads.append(grad)
                    attns[i] = blk.numpy()
                target.clear_gradient()
                
                return attns, attns_grads, label

            self.predict_fn = predict_fn