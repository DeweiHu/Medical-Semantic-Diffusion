import torch
import torch.nn as nn
import torchvision
import torch.nn.functional as F


'''
SPADE normalization
Inputs:
    seg_nch (int): channel number of the segmentation map
    output_nch (int): channel number of the output (feature channel) 
Output:
    tensor with shape [b, output_nch, h, w] (feature shape)
'''
class SPADE(nn.Module):

    def __init__(self, seg_nch, output_nch):
        super(SPADE, self).__init__()

        self.instance_norm = nn.InstanceNorm2d(output_nch, affine=False)

        self.embed_nch = 32
        self.embedding = nn.Sequential(
            nn.Conv2d(seg_nch, self.embed_nch, kernel_size=3, padding=1),
            nn.GELU(),
        )
        self.gamma = nn.Conv2d(self.embed_nch, output_nch, kernel_size=3, padding=1)
        self.beta = nn.Conv2d(self.embed_nch, output_nch, kernel_size=3, padding=1)


    def forward(self, x, segmap):
        # step 1: normalize the feature x
        x = self.instance_norm(x)

        # step 2: embed and produce scaling and bias from segmentation map
        segmap = F.interpolate(segmap, size=x.size()[2:], mode='nearest')
        seg_embed = self.embedding(segmap)
        gamma = self.gamma(seg_embed)
        beta = self.beta(seg_embed)

        # step 3: residual block
        output = x * (1 + gamma) + beta

        return output
    

class SPADE_residual_block(nn.Module):

    def __init__(self, seg_nch, input_nch, output_nch):
        super(SPADE_residual_block, self).__init__()

        hidden_nch = min(input_nch, output_nch)

        self.conv_1 = nn.Conv2d(input_nch, hidden_nch, kernel_size=3, padding=1)
        self.SPADE_norm_1 = SPADE(seg_nch, input_nch)
        
        self.conv_2 = nn.Conv2d(hidden_nch, output_nch, kernel_size=3, padding=1)
        self.SPADE_norm_2 = SPADE(seg_nch, hidden_nch)

        self.conv_3 = nn.Conv2d(input_nch, output_nch, kernel_size=3, padding=1)
        self.SPADE_norm_3 = SPADE(seg_nch, input_nch)

        self.activate = nn.GELU()


    def forward(self, x, segmap):
        main_x = self.conv_1(self.activate(self.SPADE_norm_1(x, segmap)))
        main_x = self.conv_2(self.activate(self.SPADE_norm_2(main_x, segmap)))
        side_x = self.conv_3(self.activate(self.SPADE_norm_3(x, segmap)))
        output = main_x + side_x
        
        return output


class residual_block(nn.Module):

    def __init__(self, input_nch, output_nch):
        super(residual_block, self).__init__()

        hidden_nch = min(input_nch, output_nch)
        
        self.conv_1 = nn.Conv2d(input_nch, hidden_nch, kernel_size=3, padding=1)
        self.norm_1 = nn.InstanceNorm2d(num_features=input_nch)
        
        self.conv_2 = nn.Conv2d(hidden_nch, output_nch, kernel_size=3, padding=1)
        self.norm_2 = nn.InstanceNorm2d(num_features=hidden_nch)

        self.conv_3 = nn.Conv2d(input_nch, output_nch, kernel_size=3, padding=1)
        self.norm_3 = nn.InstanceNorm2d(num_features=input_nch)

        self.activate = nn.GELU()
    

    def forward(self, x):
        main_x = self.conv_1(self.activate(self.norm_1(x)))
        main_x = self.conv_2(self.activate(self.norm_2(main_x)))
        side_x = self.conv_3(self.activate(self.norm_3(x)))
        output = main_x + side_x

        return output
    

def ConvBlock(input_nch, output_nch):
    return nn.Sequential(
            nn.Conv2d(in_channels=input_nch,
                      out_channels=output_nch,
                      kernel_size=3,
                      stride=1,
                      padding=1),
            nn.InstanceNorm2d(output_nch),
            nn.GELU(),
    )

def DownSample(input_nch, output_nch):
    return nn.Sequential(
            nn.Conv2d(in_channels=input_nch,
                      out_channels=output_nch,
                      kernel_size=4,
                      stride=2,
                      padding=1),
            nn.InstanceNorm2d(output_nch),
            nn.GELU(),
    )
            

def UpSample(input_nch, output_nch):
    return nn.Sequential(
            nn.ConvTranspose2d(in_channels=input_nch,
                               out_channels=output_nch,
                               kernel_size=4,
                               stride=2,
                               padding=1),
            nn.InstanceNorm2d(output_nch),
            nn.GELU(),
    )


class SPADE_Generator(nn.Module):

    def __init__(self, output_nch=3):
        super(SPADE_Generator, self).__init__()

        # hard code the hidden channels to be [16, 8, 4, 2], 
        self.SPADE_block_0 = SPADE_residual_block(1, 1, 16)
        self.upsample_0 = UpSample(16, 16)

        self.SPADE_block_1 = SPADE_residual_block(1, 16, 8)
        self.upsample_1 = UpSample(8, 8)

        self.SPADE_block_2 = SPADE_residual_block(1, 8, 4)
        self.upsample_2 = UpSample(4, 4)

        self.SPADE_block_3 = SPADE_residual_block(1, 4, 2)
        self.upsample_3 = UpSample(2, 2)

        self.SPADE_block_4 = SPADE_residual_block(1, 2, output_nch)


    def forward(self, x, y):
        x = self.upsample_0(self.SPADE_block_0(x, y))
        x = self.upsample_1(self.SPADE_block_1(x, y))
        x = self.upsample_2(self.SPADE_block_2(x, y))
        x = self.upsample_3(self.SPADE_block_3(x, y))
        output = self.SPADE_block_4(x, y)
        return output


class ImageEncoder(nn.Module):

    def __init__(self, input_nch, hidden_nchs, H, W):
        super(ImageEncoder, self).__init__()

        self.input_nch = input_nch
        self.hidden_nchs = hidden_nchs

        # get the feature size
        c, h, w = self.get_feature_size(H, W)
        feature_nch = c * h * w
        
        self.fc_mu = nn.Linear(feature_nch, 256)
        self.fc_var = nn.Linear(feature_nch, 256)

        self.activate = nn.GELU()

        # module list
        self.Conv = nn.ModuleList()
        self.Dsample = nn.ModuleList()

        for i in range(len(self.hidden_nchs)):
            if i  == 0:
                self.Conv.append(ConvBlock(input_nch, hidden_nchs[i]))
                self.Dsample.append(DownSample(self.hidden_nchs[i], self.hidden_nchs[i]))
            else:
                self.Conv.append(ConvBlock(hidden_nchs[i-1], hidden_nchs[i]))
                self.Dsample.append(DownSample(self.hidden_nchs[i], self.hidden_nchs[i]))


    def get_feature_size(self, H, W):
        c = self.hidden_nchs[-1]
        h = H // (2 ** (len(self.hidden_nchs)))
        w = W // (2 ** (len(self.hidden_nchs)))
        return c, h, w
        

    def forward(self, x):
        for i in range(len(self.hidden_nchs)):
            layer_output = self.Conv[i](x)
            x = self.Dsample[i](layer_output)

        x = x.view(x.size(0), -1)
        mu = self.fc_mu(x)
        logvar = self.fc_var(x)

        return mu, logvar
    

class Semantic_Generator(nn.Module):

    def __init__(self, input_nch, encoder_nchs, H, W):
        super(Semantic_Generator, self).__init__()

        self.style_encoder = ImageEncoder(input_nch=input_nch,
                                          hidden_nchs=encoder_nchs,
                                          H=H,
                                          W=W)
        self.generator = SPADE_Generator()


    def forward(self, x, y):
        mu, logvar = self.style_encoder(x.permute(0, 1, 3, 2))
        z = self.reparameterize(mu, logvar)
        fake_im = self.generator(z, y)
        return fake_im, mu, logvar


    def reparameterize(self, mu, logvar):
        std = torch.exp(0.5 * logvar)
        eps = torch.randn_like(std)
        z = mu + eps * std
        z = z.view(z.size(0), 1, 16, 16)
        return z


class Discriminator(nn.Module):

    def __init__(self, input_nch, hidden_nch=64):
        super(Discriminator, self).__init__()

        self.model = nn.Sequential(
            # layer 1
            nn.Conv2d(input_nch, hidden_nch, kernel_size=4, stride=2, padding=1),
            nn.LeakyReLU(0.2, inplace=True),
            
            # layer 2
            nn.Conv2d(hidden_nch, hidden_nch * 2, kernel_size=4, stride=2, padding=1),
            nn.BatchNorm2d(hidden_nch * 2),
            nn.LeakyReLU(0.2, inplace=True),

            # layer 3
            nn.Conv2d(hidden_nch * 2, hidden_nch * 4, kernel_size=4, stride=2, padding=1),
            nn.BatchNorm2d(hidden_nch * 4),
            nn.LeakyReLU(0.2, inplace=True),

            # layer 4
            nn.Conv2d(hidden_nch * 4, hidden_nch * 8, kernel_size=4, stride=1, padding=1),
            nn.BatchNorm2d(hidden_nch * 8),
            nn.LeakyReLU(0.2, inplace=True),

            # final layer
            nn.Conv2d(hidden_nch * 8, 1, kernel_size=4, stride=1, padding=1)
        )
    
    
    def forward(self, x):
        return self.model(x)


class VGG19(torch.nn.Module):
    def __init__(self, requires_grad=False):
        super().__init__()
        vgg_pretrained_features = torchvision.models.vgg19(pretrained=True).features
        self.slice1 = torch.nn.Sequential()
        self.slice2 = torch.nn.Sequential()
        self.slice3 = torch.nn.Sequential()
        self.slice4 = torch.nn.Sequential()
        self.slice5 = torch.nn.Sequential()
        for x in range(2):
            self.slice1.add_module(str(x), vgg_pretrained_features[x])
        for x in range(2, 7):
            self.slice2.add_module(str(x), vgg_pretrained_features[x])
        for x in range(7, 12):
            self.slice3.add_module(str(x), vgg_pretrained_features[x])
        for x in range(12, 21):
            self.slice4.add_module(str(x), vgg_pretrained_features[x])
        for x in range(21, 30):
            self.slice5.add_module(str(x), vgg_pretrained_features[x])
        if not requires_grad:
            for param in self.parameters():
                param.requires_grad = False


    def forward(self, X):
        h_relu1 = self.slice1(X)
        h_relu2 = self.slice2(h_relu1)
        h_relu3 = self.slice3(h_relu2)
        h_relu4 = self.slice4(h_relu3)
        h_relu5 = self.slice5(h_relu4)
        out = [h_relu1, h_relu2, h_relu3, h_relu4, h_relu5]
        return out


if __name__ == "__main__":
    
    device = torch.device("cuda")

    # initiate a model
    encoder_nchs = [4, 8, 16, 32, 64]
    encoder = ImageEncoder(input_nch=3, 
                           hidden_nchs=encoder_nchs,
                           H=256,
                           W=256).to(device)
    
    generator = SPADE_Generator().to(device)
    discriminator = Discriminator(4).to(device)
    
    # dimension checker
    x = torch.rand((8, 3, 256, 256), dtype=torch.float32).to(device)
    y = torch.rand((8, 1, 256, 256), dtype=torch.float32).to(device)
    mu, logvar = encoder(x)

    # reparameterization
    std = torch.exp(0.5 * logvar)
    eps = torch.randn_like(std)
    z = mu + eps * std
    z = z.view(z.size(0), 1, 16, 16)

    # SPADE Generator
    fake = generator(z, y)

    # Patch discriminator
    fake_concat = torch.cat([fake, y], dim=1)
    real_concat = torch.cat([x, y], dim=1)
    fake_and_real = torch.cat([fake_concat, real_concat], dim=0)

    discriminator_output = discriminator(fake_and_real)

    print(f"{discriminator_output.shape}")