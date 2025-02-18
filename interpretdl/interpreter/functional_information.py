import numpy as np
import os

from tqdm import tqdm
from .abc_interpreter_pytorch import InputGradientInterpreter
from ..data_processor.readers import images_transform_pipeline, preprocess_save_path
from ..data_processor.visualizer import explanation_to_vis, show_vis_explanation, save_image

from torchvision.models import resner


class FunctionalInformationInterpreter(InputGradientInterpreter):
    """
    Functional Information Interpreter.

    For input gradient based interpreters, the target issue is generally the vanilla input gradient's noises.
    The basic idea of reducing the noises is to use different similar inputs to get the input gradients and 
    do the (weighted) average. 

    Functional Information method solves the problem of meaningless local variations in partial derivatives
    by adding input-correlations-awared sampled noise to the inputs multiple times and take the (correlations-awared weighted) average of the gradients.

    Functional Information method also solves the problem of generating random noise thats not dependent on the input features 
    (pixels for images, tokens for nlp), by instead generating input-correlations-awared noise (as mentioned before).

    Intuitivly, the Functional Information of the input with respect to a specific class, 
    is high if the model's output layer class specific entry highly variates under the purtubations (adding input-correlations-aware sampled noise) for this input,
    and is low if the model's output layer class specific entry is almost constant for purtubations for this input.

    Note, the Functional Information of the input with respect to a specific class, can be vary between diffrent classes.
    """

    def __init__(self, model: callable, device: str = 'gpu:0'):
        """

        Args:
            model (callable): A model with :py:func:`forward` and possibly :py:func:`backward` functions.
            device (str): The device used for running ``model``, options: ``"cpu"``, ``"gpu:0"``, ``"gpu:1"`` 
                etc.
        """

        InputGradientInterpreter.__init__(self, model, device)


    def interpret(self,
                  inputs: str or list(str) or np.ndarray,
                  labels: list or np.ndarray = None,
                  dataset : str or list(str) or np.ndarray,
                  dataset_labels : list or np.ndarray = None,

                  noise_amount: int = 0.1,
                  n_samples: int = 50,
                  split: int = 2,
                  gradient_of: str = 'probability',
                  resize_to: int = 224,
                  crop_to: int = None,
                  visual: bool = True,
                  save_path: str = None) -> np.ndarray:
        """The technical details of FuncInfo method are described as follows:
        The method generates ``n_samples`` input-correlations-aware noised inputs, with the noise scale of ``noise_amount``, and then computes 
        the gradients *w.r.t.* these input-correlations-aware noised inputs. The final explanation is (input-correlations-aware weighted) averaged gradients. 

        Args:
            inputs (str or list): The input image filepath or a list of filepaths or numpy array of read images.
            labels (list or np.ndarray, optional): The target labels to analyze. The number of labels should be equal 
                to the number of images. If None, the most likely label for each image will be used. Default: ``None``. 
            dataset (str or list): The whole dataset filepath or a list of filepaths or numpy array of read images.  
            dataset_labels (list or np.ndarray, optional): The target labels for the whole dataset. The number of labels should be equal 
                to the dataset size. If None, the labels is infered from the dataset's filepaths names. Default: ``None``. 
            correlation_matrices (str or list): 
            save_path_corr_mat (str, optional): The filepath(s) to save/load the calculated correlation matrices. If None, the matrices will not be saved. 
                Default: ``None``. The correlation matrices in this file is for sampling correlations-aware noise, 
                for a specific class and add it to the image. Default: file path that contains a np.identity(height * width) ``identity_filepath``, 
                since the number of features of an image are its height*width, and by diffult if not taking into accont the correlation between the pixles, 
                the correlation matrix to sample the noise from is the Identity matrix.
            noise_amount (int, optional): Noise level of added correlation-aware noise to the image. The final std of the correlation-aware Gaussian random noise 
                is ``noise_amount`` * ``correlation_matrices['{class}']`` . Default: ``0.1``.
            n_samples (int, optional): The number of new images generated by adding noise. Default: ``50``.
            split (int, optional): The number of splits. Default: ``2``.
            gradient_of (str, optional): compute the gradient of ['probability', 'logit' or 'loss']. Default: 
                ``'probability'``. FuncInfo uses probability for all tasks by default.
            resize_to (int, optional): Images will be rescaled with the shorter edge being ``resize_to``. Defaults to 
                ``224``.
            crop_to (int, optional): After resize, images will be center cropped to a square image with the size 
                ``crop_to``. If None, no crop will be performed. Defaults to ``None``.
            visual (bool, optional): Whether or not to visualize the processed image. Default: ``True``.
            save_path (str, optional): The filepath(s) to save the processed image(s). If None, the image will not be 
                saved. Default: ``None``.

        Returns:
            np.ndarray: the explanation result.
        """
        assert len(data) == 1, "interpret each sample individually, it is optimized."

        if os.path.exists(save_path_corr_mat):
            print(f"Loads the correlation matrix at file path {save_path_corr_mat}")
            correlation_matrices = np.load(save_path_corr_mat, allow_pickle=True).item()
        else:
            print(f"Calculates the correlation matrix for the first time and saves it at {save_path_corr_mat}")
            correlation_matrices = self.init_corr_mat(dataset, labels = dataset_labels , specific_classes = None, resize_to, crop_to, save_path = save_path_corr_mat, visual = False) # calculated only if this class has not been calculated before
        
        imgs, data = images_transform_pipeline(inputs, resize_to, crop_to)
        print(imgs.shape, data.shape, imgs.dtype, data.dtype)  # (1, 224, 224, 3) (1, 3, 224, 224) uint8 float32

        self._build_predict_fn(gradient_of=gradient_of)

        # obtain the labels (and initialization).
        _, predicted_label, predicted_proba = self.predict_fn(data, labels)
        self.predicted_label = predicted_label
        self.predicted_proba = predicted_proba
        if labels is None:
            labels = predicted_label

        labels = np.array(labels).reshape((1, ))
        
        corr_mat  = correlation_matrices[labels[0]] # assuming single input, single label
        covariance_matrix = noise_amount * corr_mat  # Scale noise using correlation strength

        data_noised = []
        num_pixels = data.shape[2] * data.shape[3]  # Flatten (height * width)

        for i in range(n_samples):
            # Generate correlated noise (multivariate normal)
            correlated_noise = np.random.multivariate_normal(
                mean=np.zeros(num_pixels), 
                cov=covariance_matrix, 
                size=1  # Generate one sample per image
            ).reshape(1, data.shape[2], data.shape[3])  # Reshape to original image shape

            # Expand to match channels
            correlated_noise = np.repeat(correlated_noise[:, np.newaxis, :, :], 3, axis=1)  # Shape: (1, 3, 224, 224)

            # Apply noise to data
            noised_sample = data + correlated_noise.astype(np.float32)
            data_noised.append(noised_sample)

        data_noised = np.concatenate(data_noised, axis=0)
        print(data_noised.shape)  # (n_samples, 3, 224, 224)


        '''
        # SmoothGrad
        max_axis = tuple(np.arange(1, data.ndim))
        stds = noise_amount * (np.max(data, axis=max_axis) - np.min(data, axis=max_axis)) # this looks like smoothgrad

        data_noised = []
        for i in range(n_samples):
            noise = np.concatenate(
                [np.float32(np.random.normal(0.0, stds[j], (1, ) + tuple(d.shape))) for j, d in enumerate(data)])
            data_noised.append(data + noise)

        data_noised = np.concatenate(data_noised, axis=0)
        '''
        # print(data_i.shape, labels.shape)
        # print(data_noised.shape)  # n_samples, 3, 224, 224

        # splits, to avoid large GPU memory usage.
        if split > 1:
            chunk = n_samples // split
            gradient_chunks = []
            for i in range(split - 1):
                gradients_i, _, _ = self.predict_fn(data_noised[i * chunk:(i + 1) * chunk], np.repeat(labels, chunk))
                gradient_chunks.append(gradients_i)
            gradients_s, _, _ = self.predict_fn(data_noised[chunk * (split - 1):],
                                             np.repeat(labels, n_samples - chunk * (split - 1)))
            gradient_chunks.append(gradients_s)
            gradients = np.concatenate(gradient_chunks, axis=0)
        else:
            # one split.
            gradients, _, _ = self.predict_fn(data_noised, np.repeat(labels, n_samples))

        avg_gradients = np.mean(gradients, axis=0, keepdims=True)
        # visualize and save image.
        if save_path is None and not visual:
            # no need to visualize or save explanation results.
            pass
        else:
            save_path = preprocess_save_path(save_path, 1)
            # print(imgs[i].shape, avg_gradients[i].shape)
            vis_explanation = explanation_to_vis(imgs[i], np.abs(avg_gradients[0]).sum(0), style='overlay_grayscale')
            if visual:
                show_vis_explanation(vis_explanation)
            if save_path[i] is not None:
                save_image(save_path[i], vis_explanation)

        # intermediate results, for possible further usages.
        self.labels = labels

        return avg_gradients


    def init_corr_mat(self, inputs, labels, specific_classes=None, resize_to=64, crop_to=None, save_path=None, visual=False):
        """
        The covariance matrix is a crucial component of our proposed explainability method. In order to explain an output class y, 
        the covariance matrix of that class \Sigma need to be estimated empirically.

        The technical details are described as follows:
        The method gets data inputs and labels, and then computes the inputs correlation matrix *w.r.t.* each class seperatly and saves to ``save_path``.
        If one want to estimate the correlation matries for specific classes, pass ``specific_labels``.
        
        The covariance matrix may impose heavy memory requirements (the size of the covariance matrix of d-dimensional feature vectors is d^2).
        In cases of high-dimensional feature vectors, we can partition the features into subsets and sample according to the sampling protocol discussed in Sec. 4.2. 
        Alternatively, one may partition the features into subsets and assume that each subset shares the same covariance matrix. 
        For example, partitioning the features of an image into three subsets, one for each color channel, 
        and assuming all color channels share the same covariance matrix, resulting in a nine times smaller memory usage.

        Lastly, correlation matrix is required to be a positive-definite matrix. In the case where some of the features are constant 
        (e.g., the top row in the MNIST dataset is always black), or when the dimension of the feature vectors is higher than the size of the examples of class y, 
        the correlation matrix will not be a positive-definite matrix. Hence, we suggest adding a small noise to the diagonal of every correlation matrix,
        which is a well-known practice to modify the matrix to be positive-definite.

        With this function:
        Compute correlation matrices for each class in the dataset.
        Ensures positive-definiteness by adding a small noise term to the diagonal.

        Args:
            inputs (str or list): Filepaths or list of images.
            labels (list or np.ndarray): Class labels corresponding to inputs.
            specific_classes (list, optional): Specific classes for which correlation matrices should be computed. Default is all classes.
            resize_to (int, optional): Resize the shorter edge of images to this value. Default is 224.
            crop_to (int, optional): Crop images to a square of this size after resizing. Default is None.
            save_path (str, optional): Path to save the computed matrices.
            visual (bool, optional): Whether to print a part of the correlation matrix for verification.

        Returns:
            dict: A dictionary containing correlation matrices for each class.
        """

        # Transform images using the pre-processing pipeline
        imgs, data = images_transform_pipeline(inputs, resize_to, crop_to)  
        print('Shape of images used for correlation matrcies calculation:', data.shape) # should be (1, 3, 224, 224) -> (batch, channels, height, width)

        assert data.shape[1] == 3, "Expected 3 channels (RGB), but got different shape!"
        height, width = data.shape[2], data.shape[3]
        num_pixels = height * width  # 224 * 224 = 50176

        unique_classes = np.unique(labels) if specific_classes is None else specific_classes
        correlation_matrices = {}

        for class_label in unique_classes:
            print(f"Calculating correlation matrix for class {class_label}...")
            class_indices = np.where(labels == class_label)[0]
            class_data = data[class_indices]  # Select images of this class

            if class_data.shape[0] < 2:
                print(f"Skipping class {class_label} due to insufficient samples.")
                continue

            # Extract only the first channel (assuming RGB, we use Red)
            class_data = class_data[:, 0, :, :]  # Shape: (num_samples, 224, 224)

            # Reshape to (num_samples, num_pixels)
            class_data = class_data.reshape(class_data.shape[0], num_pixels)

            # Compute correlation matrix
            corr_matrix = np.corrcoef(class_data, rowvar=False)  # Shape: (num_pixels, num_pixels)

            # Ensure positive-definiteness
            epsilon = 1e-5
            corr_matrix += np.eye(num_pixels) * epsilon

            correlation_matrices[class_label] = corr_matrix

            if visual:
                print(f"Correlation matrix for class {class_label} (first 5x5 elements):\n", corr_matrix[:5, :5])

        # Save if requested
        if save_path:
            np.save(save_path, correlation_matrices)
            print(f"Correlation matrices saved to {save_path}")

        return correlation_matrices



