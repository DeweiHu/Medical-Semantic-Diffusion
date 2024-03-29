import torch
from torch import nn
import torch.nn.functional as F
from torch.optim.lr_scheduler import StepLR
import pickle
import numpy as np
from tqdm import tqdm

import utils
import unet
from dataloader import load_train_data
import diffusion_solver
import model_architecture as arch
import losses

# ----------------------------load data ------------------------------
device = torch.device("cuda")

data_path = "/home/dewei/Medical_Semantic_Diffusion/data/"
save_path = "/home/dewei/Medical_Semantic_Diffusion/result/result_diffusion/"
ckpt_path = "/home/dewei/Medical_Semantic_Diffusion/ckpt/"

with open(data_path + "OCTA_data.pickle", "rb") as handle:
    data = pickle.load(handle)

datasets = ["octa500"]
num_sample = 10
batch_size = 10
p_size = [256, 256]
intensity_range = [-1, 1]

train_data = load_train_data(data, p_size, num_sample, datasets, intensity_range, batch_size=batch_size)


# -------------------load unconditional diffusion model ------------------------------
dpm_model = unet.SimpleUnet().to(device)
dpm_model.load_state_dict(torch.load(ckpt_path + "unconditional_diffusion_octa.pt"))

for param in dpm_model.parameters():
    param.requires_grad = False

# diffusion configuration
beta_start = 0.0001
beta_end = 0.02
T = 300
betas = diffusion_solver.get_beta_schedule(beta_schedule="linear",
                                           beta_start=beta_start,
                                           beta_end=beta_end,
                                           num_diffusion_timesteps=T)

sampler = diffusion_solver.DiffusionSampler(betas, device=device)


# -------------------load segmentation model ------------------------------
seg_model = arch.res_Unet().to(device)
seg_model.load_state_dict(torch.load(ckpt_path + "segmentor_octa500.pt"))
initial_state = {name: param.clone() for name, param in seg_model.named_parameters()}


# training configuration
n_epoch = 10
DSC_loss = losses.DiceBCELoss()
CE_loss = nn.CrossEntropyLoss()

lr = 1e-4
optimizer = torch.optim.Adam(seg_model.parameters(), lr=lr)
scheduler = StepLR(optimizer, step_size=5, gamma=0.5)


# -------------------------- training ------------------------------
softmax = nn.Softmax2d()
strength = 850

for epoch in range(n_epoch):
    for step, (x, y) in enumerate(train_data):
        
        seg_model.train()
        
        x_t = torch.randn((batch_size, 1, 256, 256))
        y = y.squeeze(1).to(torch.long).to(device)
        
        # unconditional diffusion
        x_0_uncondition = sampler.reverse_iterate(x_t, T-1, dpm_model)
        x_0_uncondition = torch.clamp(x_0_uncondition, -1.0, 1.0)

        # classifier-guided conditional diffusion
        values = range(T)
        with tqdm(total=len(values)) as pbar:
        
            for j in range(0, T)[::-1]:
                optimizer.zero_grad()

                t_tensor = torch.full((1, ), j, dtype=torch.long)
                
                # compute x_{0|t} for each timestep t
                x_0_t = sampler.reverse_skip(x_t, t_tensor, dpm_model)
                x_0_t = torch.clamp(x_0_t, 0.0, 1.0).to(device)

                # conduct segmentation 
                pred = seg_model(x_0_t)
                pred_y = torch.argmax(softmax(pred), dim=1)

                # update the segmentation network parameters
                loss = CE_loss(pred, y) + DSC_loss(pred_y, y)
                loss.backward()
                optimizer.step()

                # compute gradient with regard to x_{0|t}. 
                # Do the forward process again
                x_in = x_0_t.clone().detach().requires_grad_(True)

                pred = seg_model(x_in)
                pred_y = torch.argmax(softmax(pred), dim=1)
                loss = CE_loss(pred, y) + DSC_loss(pred_y, y)
                gradient = torch.autograd.grad(- strength * loss, x_in)[0]

                # update x_t with x_{t-1}
                mean, std, epsilon = sampler.reverse_sample_typeII(x_t, 
                                                                   t_tensor, 
                                                                   dpm_model, 
                                                                   output_type='gaussian')
                mean_shift = mean + gradient.cpu()
                x_t = mean_shift + std * epsilon

                pbar.update(1)
                pbar.set_description("step: %d, loss: %.4f, grad: %.4f" \
                                     %(step, loss.item(), torch.norm(gradient).item()))

                # update checker
                for name, param in seg_model.named_parameters():
                    assert not torch.allclose(param, initial_state[name]), f"Parameter {name} was not updated!"

            # final output of conditional diffusion
            x_0_condition = torch.clamp(x_t, -1.0, 1.0)
            im_y = y[0].detach().cpu().numpy()
            im_pred_y = pred_y[0].detach().cpu().numpy()

            # make a plot
            im_uncondition = np.array(utils.tensor2pil(x_0_uncondition))[:,:,0]
            im_condition = np.array(utils.tensor2pil(x_0_condition))[:,:,0]
            im_y = np.uint8(utils.ImageRescale(im_y, [0, 255]))
            im_pred_y = np.uint8(utils.ImageRescale(im_pred_y, [0, 255]))

            im_plot = np.concatenate((im_uncondition, im_condition, im_pred_y, im_y), axis=1)
            name = f"st{step}"
            utils.image_saver(im_plot, save_path, name)

        # scheduler.step()

    name = "pretrained_seg_octa500.pt"
    torch.save(seg_model.state_dict(), ckpt_path + name)