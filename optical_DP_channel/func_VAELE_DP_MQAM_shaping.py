################################################################################################
# Author:        Vincent Lauinger, 
# Affiliation:   Communications Engineering Lab (CEL), Karlsruhe Institute of Technology (KIT)
# Contact:       vincent.lauinger@kit.edu
# Last revision: 15th of June 2022
################################################################################################

import numpy as np
import torch
import torch.optim as optim

import matplotlib.pyplot as plt

import shared_funcs as sfun


def processing(mod, sps, SNR, nu, M_est, theta_diff,theta,lr_optim, batch_len, N_frame_max, num_frames, flex_step, channel, symb_rate,tau_cd,tau_pmd,phiIQ, N_lrhalf):
    device = 'cpu' if torch.cuda.is_available() else 'cpu'
    print("We are using the following device for learning:",device)

    h_est, h_channel, P, amp_levels, amps, pol, nu_sc, var,pow_mean = sfun.init(channel, mod, device, nu, sps, M_est, SNR)
    num_lev = amp_levels.shape[0]
    P_tensor = torch.tensor(P, device=device, dtype=torch.float32)  

    # initialize net (butterfly FIR)
    net = sfun.twoXtwoFIR(M_est,sps).to(device)
    # initialize optimizer
    optimizer = optim.Adam(net.parameters(), lr= lr_optim)

    # add h_est as parameter
    optimizer.add_param_group({"params": h_est})

    SER_valid = torch.empty(4,num_frames, device=device, dtype=torch.float32)
    Var_est = torch.empty(pol,num_frames, device=device, dtype=torch.float32)

    minibatch = torch.empty(pol,2,batch_len*sps, device=device, dtype=torch.float32)

    m_max = N_frame_max//batch_len
    N_frame = m_max * batch_len
    N_cut = 10      # number of symbols cut off to prevent edge effects of convolution


    for frame in range(num_frames):

        if (frame%N_lrhalf == 0 and frame!=0):      # learning rate scheduler
            optimizer.param_groups[0]['lr'] = lr_optim * 0.5    

        net.train()
        with torch.set_grad_enabled(True):
            rx_tensor, data_tensor, sigma_n = sfun.generate_data_shaping(N_frame, amps, SNR, h_channel, P, pol,symb_rate,sps,tau_cd,tau_pmd,phiIQ,theta, device)
            theta += theta_diff     # update theta per frame

            out_train = torch.empty(pol,2*num_lev,N_frame, device=device, dtype=torch.float32, requires_grad=False)
            out_const = torch.empty(pol,2,N_frame, device=device, dtype=torch.float32, requires_grad=False)
            var_est = torch.empty(pol,m_max, device=device, dtype=torch.float32, requires_grad=False)

            for m in range(m_max):
                minibatch[:,:,:] = rx_tensor[:,:,m*batch_len*sps:(m+1)*batch_len*sps]
                optimizer.zero_grad()
                minibatch_output, out_zf = net(minibatch, amp_levels, var, nu_sc) # minibatch.roll(1,-1)
                out_train[:,:,m*batch_len:(m+1)*batch_len] = minibatch_output.detach().clone()
                out_const[:,:,m*batch_len:(m+1)*batch_len] = out_zf.detach().clone()

                loss, var_est[:,m] = sfun.loss_function_shaping(minibatch_output.squeeze(), minibatch.squeeze(), h_est, amp_levels, P_tensor)
                loss.backward()
                optimizer.step()  # optimize per minibatch

        SNR_est = pow_mean/torch.mean(var_est)     # SNR estimation
        Var_est[:,frame] = torch.mean(var_est,dim=1)  
        shift,r = sfun.find_shift(out_train,data_tensor, 21, amp_levels, pol)   # find correlation within 21 symbols
        out_train = out_train.roll(r,0)     # compensate pol. shift
        out_train[0,:,:], out_train[1,:,:] = out_train[0,:,:].roll(int(-shift[0]),-1), out_train[1,:,:].roll(int(-shift[1]),-1)     # compensate time shift (in multiple symb.)
        temp_out_train_reshape = out_train.reshape(pol,2*num_lev,m_max,batch_len) 
        temp_out_train_cut = temp_out_train_reshape[:,:,:,:batch_len-shift[0]-N_cut]        # cut off edge symbols to avoid edge effects
        temp_out_train = temp_out_train_cut.reshape(pol,2*num_lev,-1)
    
        temp_data_tensor = (data_tensor.reshape(pol,2,m_max,batch_len)[:,:,:,:batch_len-shift[0]-N_cut]).reshape(pol,2,-1)

        SER_valid[2:,frame] = sfun.SER_IQflip(temp_out_train[:,:,11:-11-torch.max(torch.abs(shift))], temp_data_tensor[:,:,11:-11-torch.max(torch.abs(shift))])
            
        shift,r = sfun.find_shift_symb_full(out_const,data_tensor, 21)      # find correlation within 21 symbols
        out_const = out_const.roll(r,0)     # compensate pol. shift
        out_const[0,:,:], out_const[1,:,:] = out_const[0,:,:].roll(int(-shift[0]),-1), out_const[1,:,:].roll(int(-shift[1]),-1)     # compensate time shift (in multiple symb.)
        temp_out_const_reshape = out_const.reshape(pol,2,m_max,batch_len) 
        temp_out_const_cut = temp_out_const_reshape[:,:,:,:batch_len-shift[0]-N_cut]        # cut off edge symbols to avoid edge effects
        temp_out_const= temp_out_const_cut.reshape(pol,2,-1)
        temp_data_tensor = (data_tensor.reshape(pol,2,m_max,batch_len)[:,:,:,:batch_len-shift[0]-N_cut]).reshape(pol,2,-1)
        
        SER_valid[:2,frame] = sfun.SER_constell_shaping(temp_out_const[:,:,11:-11-torch.max(torch.abs(shift))].detach().clone(), temp_data_tensor[:,:,11:-11-torch.max(torch.abs(shift))], amp_levels, nu_sc, var) 

        print(frame,'\t\ttraining: loss = ', loss.item(), '\tshift_x = ',  shift[0].item(), '\tshift_y = ',  shift[1].item(), '\tr = ',  r , '\tSNR_est = ',  (10*torch.log10(SNR_est)).item() )  
        print('\t\t\t\t\t\t\tSER_x = ',  SER_valid[0,frame].item(), '\tSER_y = ',  SER_valid[1,frame].item(), '\t(constell. with shaping)' ) 
        print('\t\t\t\t\t\t\tSER_x = ',  SER_valid[2,frame].item(), '\tSER_y = ',  SER_valid[3,frame].item(), '\t(soft demapper)' ) 

    return SER_valid, Var_est, var



def plot_constellation(E):
    fontSize = 35
    fig = plt.figure(figsize=(12, 10))
    ax = fig.add_subplot(1, 1, 1)
    if np.iscomplexobj(E) == True:
        if E.shape[0] == 2:
            ax.scatter(E[0].real, E[0].imag, s = 1, c='red', alpha=0.5, label="X")
            ax.scatter(E[1].real, E[1].imag, s = 1, c='blue', alpha=0.5, label="Y")
        else:
            ax.scatter(E.real, E.imag, s = 1, c='red', alpha=0.5, label="X")
    else:
        if E.shape[1] == 2:
            if E.shape[0] == 2:
                ax.scatter(E[0,0,:], E[0,1,:], s = 3, c='red', alpha=0.8, label="X")
                ax.scatter(E[1,0,:], E[1,1,:], s = 3, c='blue', alpha=0.8, label="Y")
            else:
                ax.scatter(E[0,0,:], E[0,1,:], s = 3, c='red', alpha=0.8, label="X")
        else:
            ax.scatter(E[0,:], E[1,:], s = 3, c='red', alpha=0.8, label="X")

    plt.legend(loc='best', fontsize=fontSize)
    plt.xlabel('In-Phase', fontsize=fontSize)
    plt.ylabel('Quadrature', fontsize=fontSize)
    plt.xticks(fontsize=fontSize)
    plt.yticks(fontsize=fontSize)
    plt.grid(True)

    plt.show()



