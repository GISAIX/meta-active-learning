import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn import init

from vae import VariationalAutoencoder, Encoder, Decoder

def softmax(input, dim=1):
    #input=torch.autograd.Variable(input) # not needed

    input_size = input.size()
    
    trans_input = input.transpose(dim, len(input_size)-1)
    trans_size = trans_input.size()

    input_2d = trans_input.contiguous().view(-1, trans_size[-1])
    
    soft_max_2d = F.softmax(input_2d)
    
    soft_max_nd = soft_max_2d.view(*trans_size)
    return soft_max_nd.transpose(dim, len(input_size)-1)

class Classifier(nn.Module):
    """
    Two layer classifier
    with softmax output.
    """
    def __init__(self, dims):
        super(Classifier, self).__init__()
        [x_dim, h_dim, y_dim] = dims
        self.dense = nn.Linear(x_dim, h_dim)
        self.logits = nn.Linear(h_dim, y_dim)
        self.output_activation = softmax #F.softmax

    def forward(self, x):
        x = F.softplus(self.dense(x))
        x = self.output_activation(self.logits(x), dim=-1)
        return x


class DeepGenerativeModel(VariationalAutoencoder):
    """
    M2 code replication from the paper
    'Semi-Supervised Learning with Deep Generative Models'
    (Kingma 2014) in PyTorch.

    The "Generative semi-supervised model" is a probabilistic
    model that incorporates label information in both
    inference and generation.
    """
    def __init__(self, dims, ratio):
        """
        Initialise a new generative model
        :param ratio: ratio between labelled and unlabelled data
        :param dims: dimensions of x, y, z and hidden layers.
        """
        self.alpha = 0.1 * ratio

        [x_dim, self.y_dim, z_dim, h_dim] = dims
        super(DeepGenerativeModel, self).__init__([x_dim, z_dim, h_dim])

        self.encoder = Encoder([x_dim + self.y_dim, h_dim, z_dim])
        self.decoder = Decoder([z_dim + self.y_dim, list(reversed(h_dim)), x_dim])
        self.classifier = Classifier([x_dim, h_dim[0], self.y_dim])

        for m in self.modules():
            if isinstance(m, nn.Linear):
                init.xavier_normal(m.weight.data)
                if m.bias is not None:
                    m.bias.data.zero_()

    def forward(self, x, y=None):
        logits = self.classifier(x)

        if y is None:
            return logits

        # Add label and data and generate latent variable
        z, z_mu, z_log_var = self.encoder(torch.cat([x, y], dim=1))

        # Reconstruct data point from latent data and label
        reconstruction = self.decoder(torch.cat([z, y], dim=1))

        return reconstruction, logits, [[z, z_mu, z_log_var]]

    def sample(self, z, y):
        """
        Samples from the Decoder to generate an x.
        :param z: Latent normal variable
        :param y: label (one-hot encoded)
        :return: x
        """
        y = y.type(torch.FloatTensor)
        x = self.decoder(torch.cat([z, y], dim=1))
        return x


class StackedDeepGenerativeModel(DeepGenerativeModel):
    """
    M1+M2 model as described in Kingma 2014.
    """
    def __init__(self, dims, ratio, features):
        """
        Initialise a new stacked generative model
        :param ratio: ratio between labelled and unlabelled data
        :param dims: dimensions of x, y, z and hidden layers
        :param features: a pretrained M1 model of class VariationalAutoencoder
            trained on the same dataset.
        """
        [x_dim, y_dim, z_dim, h_dim] = dims
        super(StackedDeepGenerativeModel, self).__init__([features.z_dim, y_dim, z_dim, h_dim], ratio)

        # Be sure to reconstruct with the same dimensions
        in_features = self.decoder.reconstruction.in_features
        self.decoder.reconstruction = nn.Linear(in_features, x_dim)

        # Make vae feature model untrainable by freezing parameters
        self.features = features
        self.features.train(False)

        for param in self.features.parameters():
            param.requires_grad = False

    def forward(self, x, y=None):
        # Sample a new latent x from the M1 model
        x_sample, _, _ = self.features.encoder(x)

        # Use the sample as new input to M2
        return super(StackedDeepGenerativeModel, self).forward(x_sample, y)


class AuxiliaryDeepGenerativeModel(VariationalAutoencoder):
    """
    Auxiliary Deep Generative Models (Maaløe 2016)
    code replication. The ADGM introduces an additional
    latent variable 'a', which enables the model to fit
    more complex variational distributions.
    """
    def __init__(self, ratio, dims):
        self.alpha = 0.1
        self.beta = self.alpha * ratio

        [self.x_dim, self.y_dim, self.z_dim, self.h_dim] = dims
        super(AuxiliaryDeepGenerativeModel, self).__init__([self.x_dim, self.z_dim, self.h_dim])

        self.aux_encoder = Encoder([self.x_dim, self.h_dim, self.z_dim])
        self.encoder = Encoder([self.z_dim, self.h_dim, self.z_dim])

        self.aux_decoder = Decoder([self.z_dim, list(reversed(self.h_dim)), self.z_dim])
        self.decoder = Decoder([self.z_dim, list(reversed(self.h_dim)), self.x_dim])

        self.classifier = Classifier([self.x_dim, self.h_dim[0], self.y_dim])

        # Transform layers
        self.transform_x_to_z = nn.Linear(self.x_dim, self.z_dim)
        self.transform_y_to_z = nn.Linear(self.y_dim, self.z_dim)
        self.transform_z_to_x = nn.Linear(self.z_dim, self.x_dim)

    def forward(self, x, y=None):
        """
        Forward through the model
        :param x: features
        :param y: labels
        :return: reconstruction, logits, [z], [a]
        """
        # Auxiliary inference q(a|x)
        a, a_mu, a_log_var = self.aux_encoder(x)

        # Classification q(y|a,x)
        logits = self.classifier(self.transform_z_to_x(a) + x)

        if y is None:
            return logits

        # Latent inference q(z|a,y,x)
        z, z_mu, z_log_var = self.encoder(a + self.transform_y_to_z(y) + self.transform_x_to_z(x))

        # Generative p(a|z,y)
        a = self.aux_decoder(z + self.transform_y_to_z(y))

        # Generative p(x|a,z,y)
        reconstruction = self.decoder(a + z + self.transform_y_to_z(y))

        return reconstruction, logits, [z, z_mu, z_log_var], [a, a_mu, a_log_var]

    def sample(self, z, a, y):
        """
        Samples from the Decoder to generate an x.
        :param z: Latent normal variable
        :param a: Auxiliary normal variable
        :param y: label
        :return: x
        """
        a = a.type(torch.FloatTensor)
        y = y.type(torch.FloatTensor)
        y = self.transform_y_to_z(y)
        return self.decoder(z + a + y)