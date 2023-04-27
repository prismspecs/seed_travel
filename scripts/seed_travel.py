import gradio as gr
import imageio
import math
import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt
import numpy as np
import os
from PIL import Image
import random
import re
import sys
import torch
from torchmetrics import StructuralSimilarityIndexMeasure
from torchvision import transforms
from torch.nn import functional as F
import modules.scripts as scripts
from modules.processing import Processed, process_images, fix_seed
from modules.shared import opts, cmd_opts, state, sd_upscalers
from modules.images import resize_image
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from rife.ssim import ssim_matlab
from rife.RIFE_HDv3 import Model

__ = lambda key, value=None: opts.data.get(f'customscript/seed_travel.py/txt2img/{key}/value', value)

DEFAULT_UPSCALE_METH   = __('Upscaler', 'Lanczos')
DEFAULT_UPSCALE_RATIO  = __('Upscale ratio', 1.0)
CHOICES_UPSCALER  = [x.name for x in sd_upscalers]

class Script(scripts.Script):
    def title(self):
        return "Seed travel"

    def show(self, is_img2img):
        return True

    def ui(self, is_img2img):
        seed_travel_extra = []

        dest_seed = gr.Textbox(label='Destination seeds', lines=1)
        with gr.Row():
            rnd_seed = gr.Checkbox(label='Use random seeds', value=False)
            seed_count = gr.Number(label='Number of random seeds', value=4)
        with gr.Row():
            steps = gr.Number(label='Steps', value=10)
            loopback = gr.Checkbox(label='Loop back to initial seed', value=False)
        with gr.Row():
            video_fps = gr.Number(label='FPS', value=30)
            lead_inout = gr.Number(label='Lead in/out', value=0)
        with gr.Row():
            ssim_diff = gr.Slider(label='SSIM threshold', info='0 to disable', value=0.0, minimum=0.0, maximum=1.0, step=0.01)
            ssim_ccrop = gr.Slider(label='SSIM CenterCrop%', info='0 to disable', value=0, minimum=0, maximum=100, step=1)
        with gr.Row():
            rife_passes = gr.Number(label='RIFE passes', value=0)
            rife_drop = gr.Checkbox(label='Drop original frames', value=False)
        with gr.Row():
            curve = gr.Dropdown(label='Interpolation curve', value='Linear', choices=[
                'Linear', 'Hug-the-middle', 'Hug-the-nodes', 'Slow start', 'Quick start', 'Easy ease in', 'Partial', 'Random'
                ])
            curvestr = gr.Slider(label='Curve strength', value=3, minimum=0.0, maximum=10.0, step=0.1)
        with gr.Accordion(label='Seed Travel Extras...', open=False):
            gr.HTML(value='Seed Travel links: <a href=http://github.com/yownas/seed_travel/>Github</a>')
            with gr.Row():
                upscale_meth  = gr.Dropdown(label='Upscaler',    value=lambda: DEFAULT_UPSCALE_METH, choices=CHOICES_UPSCALER)
                upscale_ratio = gr.Slider(label='Upscale ratio', value=lambda: DEFAULT_UPSCALE_RATIO, minimum=0.0, maximum=8.0, step=0.1)
            with gr.Row():
                use_cache = gr.Checkbox(label='Use cache', value=True)
                show_images = gr.Checkbox(label='Show generated images in ui', value=True)
            allowdefsampler = gr.Checkbox(label='Allow default sampler', value=False)
            compare_paths = gr.Checkbox(label='Compare paths', value=False)
            bump_seed = gr.Slider(label='Bump seed', value=0.0, minimum=0, maximum=0.5, step=0.001)
            substep_min = gr.Number(label='SSIM min substep', value=0.001)
            ssim_diff_min = gr.Slider(label='SSIM min threshold', value=75, minimum=0, maximum=100, step=1)
            save_stats = gr.Checkbox(label='Save extra status information', value=True)

        return [rnd_seed, seed_count, dest_seed, steps, curve, curvestr, loopback, video_fps,
                show_images, compare_paths, allowdefsampler, bump_seed, lead_inout, upscale_meth, upscale_ratio,
                use_cache, ssim_diff, ssim_ccrop, substep_min, ssim_diff_min, rife_passes, rife_drop, save_stats]

    def get_next_sequence_number(path):
        from pathlib import Path
        """
        Determines and returns the next sequence number to use when saving an image in the specified directory.
        The sequence starts at 0.
        """
        result = -1
        dir = Path(path)
        for file in dir.iterdir():
            if not file.is_dir(): continue
            try:
                num = int(file.name)
                if num > result: result = num
            except ValueError:
                pass
        return result + 1

    def run(self, p, rnd_seed, seed_count, dest_seed, steps, curve, curvestr, loopback, video_fps,
            show_images, compare_paths, allowdefsampler, bump_seed, lead_inout, upscale_meth, upscale_ratio,
            use_cache, ssim_diff, ssim_ccrop, substep_min, ssim_diff_min, rife_passes, rife_drop, save_stats):
        initial_info = None
        images = []
        lead_inout=int(lead_inout)
        if upscale_meth != 'None' and upscale_ratio != 1.0 and upscale_ratio != 0.0:
            tgt_w, tgt_h = round(p.width * upscale_ratio), round(p.height * upscale_ratio)
        else:
            tgt_w, tgt_h = p.width, p.height
        video_fps = 0 if video_fps == None else video_fps
        save_video = video_fps != 0

        def st_fixseed(seed):
            if seed == -1:
                return random.randint(0,2147483647)
            else:
                return seed

        # If we are just bumping seeds, ignore compare_paths and save_video
        if bump_seed > 0:
            compare_paths = False
            save_video = False
            steps = 1
            allowdefsampler = True # Since we aren't trying to get to a target seed, this will be ok.

        if not allowdefsampler and p.sampler_name == 'Euler a':
            print(f"You seem to be using Euler a, it will most likely not produce good results.")
            return Processed(p, images, p.seed)

        if rnd_seed and (not seed_count or int(seed_count) < 2):
            print(f"You need at least 2 random seeds.")
            return Processed(p, images, p.seed)

        if not rnd_seed and not dest_seed:
            print(f"No destination seeds were set.")
            return Processed(p, images, p.seed)

        if not save_video and not show_images:
            print(f"Nothing to show in gui. You will find the result in the output folder.")

        # Remove seeds within () to help testing
        dest_seed = re.sub('\([^)]*\)', ',', dest_seed)
        dest_seed = re.sub(',,*', ',', dest_seed)

        # Custom seed travel saving
        travel_path = os.path.join(p.outpath_samples, "travels")
        os.makedirs(travel_path, exist_ok=True)
        travel_number = Script.get_next_sequence_number(travel_path)
        travel_path = os.path.join(travel_path, f"{travel_number:05}")
        p.outpath_samples = travel_path
        if save_video: os.makedirs(travel_path, exist_ok=True)

        # Force Batch Count and Batch Size to 1.
        p.n_iter = 1
        p.batch_size = 1
        p.seed = int(p.seed)

        initial_prompt = p.prompt
        initial_negative_prompt = p.negative_prompt

        if compare_paths or bump_seed > 0:
            loopback = False

        seeds = []
        # Random seeds
        if rnd_seed == True:
            if (compare_paths or bump_seed) and not p.seed == None:
                seeds.append(p.seed)
            s = 0          
            while (s < seed_count):
                seeds.append(random.randint(0,2147483647))
                s = s + 1
        # Manual seeds        
        else:
            seeds = [] if p.seed == None else [st_fixseed(p.seed)]
            seeds = seeds + [st_fixseed(int(x.strip())) for x in dest_seed.split(",")]
        p.seed = seeds[0]

        if bump_seed > 0:
            p.subseed_strength = bump_seed
            for s in range(len(seeds)-1):
                if state.interrupted:
                    break
                p.subseed = seeds[s+1]
                fix_seed(p)
                seeds[s] = p.seed
                seeds[s+1] = p.subseed
                proc = process_images(p)
                if initial_info is None:
                    initial_info = proc.info
                images += proc.images
            return Processed(p, images if show_images else [], p.seed, initial_info)

        travel_queue = []
        if compare_paths:
            travel_queue = [[seeds[0], seeds[i+1]] for i in range(len(seeds)-1)]
        else:
            travel_queue = [[seeds[i] for i in range(len(seeds))]]

        generation_queues = []
        for travel in travel_queue:
            generation_queue = []
            for s in range(len(travel) - (0 if loopback else 1)):
                p.seed = travel[s]
                p.subseed = travel[s+1] if s+1 < len(travel) else travel[0]
                fix_seed(p) # replaces None and -1 with random seeds
                travel[s] = p.seed
                if s+1 < len(travel):
                    travel[s+1] = p.subseed
                else:
                    travel[0] = p.subseed
                seed, subseed = p.seed, p.subseed
                numsteps = int(steps) + (1 if s+1 == len(travel) else 0)
                for i in range(numsteps):
                    strength = float(i/float(steps))

                    # Calculate curve
                    if curve == "Hug-the-middle":
                        # https://www.wolframalpha.com/input?i=graph+x%2B%28s%2F30%29*sin%28x*pi*2%29+from+0+to+1%2C+s%3D3
                        strength = strength + (curvestr/30.0 * math.sin(strength*2*math.pi))
                    elif curve == "Hug-the-nodes":
                        # https://www.wolframalpha.com/input?i=graph+x-%28s%2F30%29*sin%28x*pi*2%29+from+0+to+1%2C+s%3D3
                        strength = strength - (curvestr/30.0 * math.sin(strength*2*math.pi))
                    elif curve == "Slow start":
                        # https://www.wolframalpha.com/input?i=graph+x%5Es+from+0+to+1%2C+s%3D3
                        strength = strength**curvestr
                    elif curve == "Quick start":
                        # https://www.wolframalpha.com/input?i=graph+%281-x%29%5Es+from+0+to+1%2C+s%3D3
                        strength = (1-strength)**curvestr
                    elif curve == "Easy ease in":
                        # https://www.wolframalpha.com/input?i=graph+%281-cos%28x%5E%28s*pi%2F10%29*pi%29%29%2F2+from+0+to+1%2C+s%3D3
                        strength = (1-math.cos(strength**(curvestr*math.pi)*math.pi))/2.0
                    elif curve == "Partial":
                        # "Travel" part way before switching to next seed
                        strength = strength*curvestr/10.0
                    elif curve == "Random":
                        # Random flicker before switching to next seed
                        strength = random.uniform(0, curvestr/10.0)
                    # "Linear" is default (do nothing) https://www.wolframalpha.com/input?i=graph+x+from+0+to+1

                    key = (seed, subseed, strength)
                    generation_queue.append(key)
            if not loopback: # Kludge to add last image
                key = (subseed, subseed, 0.0)
                generation_queue.append(key)
            generation_queues.append(generation_queue)

        if bump_seed:
            total_images = len(seeds)
        else:
            total_images = len(set(key for queue in generation_queues for key in queue))
        print(f"Generating {total_images} images.")

        # Set generation helpers
        state.job_count = total_images

        # reuse generated images with the same seeds and strength
        image_cache = {}

        for s in range(len(generation_queues)):
            queue = generation_queues[s]
            step_images = []
            step_keys = []
            for key in queue:
                if state.interrupted:
                    break
                p.seed, p.subseed, p.subseed_strength = key
                step_keys += [key]

                # DEBUG
                print(f"Process: {key} of {seeds}")

                # lower seed comes first so equivalent cached images hash the same
                # e.g. strength 0.75 from B to A = strength 0.25 from A to B
                seed0, seed1, strength = key
                if seed1 < seed0:
                    seed0, seed1 = seed1, seed0
                    strength = 1 - strength
                if strength == 0: seed1 = 0 # seed1 does not affect the output when strength is 0
                if strength == 1: seed0 = 0 # seed0 does not affect the output when strength is 1
                cache_key = (seed0, seed1, strength)

                if use_cache and cache_key in image_cache:
                    step_images += image_cache[cache_key]
                    images += image_cache[cache_key]
                    continue

                proc = process_images(p)
                if initial_info is None:
                    initial_info = proc.info

                # upscale - copied from https://github.com/Kahsolt/stable-diffusion-webui-prompt-travel
                if upscale_meth != 'None' and upscale_ratio != 1.0 and upscale_ratio != 0.0:
                    image = [resize_image(0, proc.images[0], tgt_w, tgt_h, upscaler_name=upscale_meth)]
                else:
                    image = [proc.images[0]]

                step_images += image
                images += image
                if use_cache:
                    image_cache[cache_key] = image

            # If SSIM > 0 and not bump_seed
            if ssim_diff > 0:
                ssim = StructuralSimilarityIndexMeasure(data_range=1.0)
                if ssim_ccrop == 0:
                    transform = transforms.Compose([transforms.ToTensor()])
                else:
                    transform = transforms.Compose([transforms.CenterCrop((tgt_h*(ssim_ccrop/100), tgt_w*(ssim_ccrop/100))), transforms.ToTensor()])

                check = True
                skip_count = 0
                not_better = 0
                skip_ssim_min = 1.0
                min_step = 1.0
                ssim_stats = {}
                ssim_stats_new = {}

                done = 0
                while(check):
                    if state.interrupted:
                        break
                    check = False
                    for i in range(done, len(step_images)-1):
                        # Check distance between i and i+1

                        a = transform(step_images[i].convert('RGB')).unsqueeze(0)
                        b = transform(step_images[i+1].convert('RGB')).unsqueeze(0)
                        d = ssim(a, b)

                        seed_a, subseed_a, subseed_strength_a = step_keys[i]
                        seed_b, subseed_b, subseed_strength_b = step_keys[i+1]
                        if subseed_strength_b == 0: # If next image is the start of a new seed...
                            subseed_strength_b = 1

                        if d < ssim_diff and abs(subseed_strength_b - subseed_strength_a) > substep_min:
                            # DEBUG
                            print(f"SSIM: {step_keys[i]} <-> {step_keys[i+1]} = ({subseed_strength_b - subseed_strength_a}) {d}")

                            # Add image and run check again
                            check = True

                            new_strength = (subseed_strength_a + subseed_strength_b)/2.0
                            key = (seed_a, subseed_a, new_strength)
                            p.seed, p.subseed, p.subseed_strength = key

                            # SSIM stats for the new image
                            ssim_stats_new[(step_keys[i], (step_keys[i+1][0], step_keys[i+1][1], subseed_strength_b))] = d

                            if min_step > (subseed_strength_b - subseed_strength_a)/2.0:
                                min_step = (subseed_strength_b - subseed_strength_a)/2.0

                            # DEBUG
                            print(f"Process: {key} of {seeds}")
                            proc = process_images(p)

                            if initial_info is None:
                                initial_info = proc.info

                            # upscale - copied from https://github.com/Kahsolt/stable-diffusion-webui-prompt-travel
                            if upscale_meth != 'None' and upscale_ratio != 1.0 and upscale_ratio != 0.0:
                                image = resize_image(0, proc.images[0], tgt_w, tgt_h, upscaler_name=upscale_meth)
                            else:
                                image = proc.images[0]

                            # Check if this was an improvment
                            c = transform(image.convert('RGB')).unsqueeze(0)
                            d2 = ssim(a, c)

                            if d2 > d or d2 < ssim_diff*ssim_diff_min/100.0:
                                # Keep image if it is improvment or hasn't reached desired min ssim_diff
                                step_images.insert(i+1, image)
                                step_keys.insert(i+1, key)
                            else:
                                print(f"Did not find improvment: {d2} < {d} ({d-d2}) Taking shortcut.")
                                not_better += 1
                                done = i + 1

                            break;
                        else:
                            # DEBUG
                            if d > ssim_diff:
                                if i > done:
                                    print(f"Done: {step_keys[i]} <-> {step_keys[i+1]} => ({subseed_strength_b - subseed_strength_a}) {d} ({i} of {len(step_keys)} frames)")

                            else:
                                print(f"Reached minimum step limit @{step_keys[i]} (Skipping) SSIM = {d}  ")
                                if skip_ssim_min > d:
                                    skip_ssim_min = d
                                skip_count += 1
                            done = i
                            ssim_stats[(step_keys[i], step_keys[i+1])] = d

                # DEBUG
                print("SSIM done!")
                print(f"Stats: Skip count: {skip_count} Worst: {skip_ssim_min} No improvment: {not_better} Min. step: {min_step}")

            # Save video before continuing with SSIM-stats and RIFE (If things crashes we will atleast have this video)
            if save_video:
                try:
                    frames = [np.asarray(step_images[0])] * lead_inout + [np.asarray(t) for t in step_images] + [np.asarray(step_images[-1])] * lead_inout
                    fps = video_fps if video_fps > 0 else len(frames) / abs(video_fps)
                    filename = f"travel-{travel_number:05}-{s:04}.mp4" if compare_paths else f"travel-{travel_number:05}.mp4"
                    writer = imageio.get_writer(os.path.join(travel_path, filename), fps=fps, quality=8)
                    for frame in frames:
                        writer.append_data(frame)
                    writer.close()
                except:
                    print(f"ERROR: Failed generating video")

            # SSIM-stats
            if save_stats and ssim_diff > 0:
                # Create scatter plot
                x = []
                y = []
                for i in ssim_stats_new:
                    s = i[1][2] - i[0][2]
                    if s > 0:
                        x.append(s) # step distance
                        y.append(ssim_stats_new[i]) # ssim
                plt.scatter(x, y, s=1, color='#ffa600')
                x = []
                y = []
                for i in ssim_stats:
                    s = i[1][2] - i[0][2]
                    if s > 0:
                        x.append(s) # step distance
                        y.append(ssim_stats[i]) # ssim
                plt.scatter(x, y, s=1, color='#003f5c')
                plt.axvline(substep_min)
                plt.axhline(ssim_diff)

                plt.xscale('log')
                plt.title('SSIM scatter plot')
                plt.xlabel('Step distance')
                plt.ylabel('SSIM')
                filename = f"ssim_scatter-{travel_number:05}.svg"
                plt.savefig(os.path.join(travel_path, filename))
                plt.close()

            # Save settings and other information
            if save_stats:
                D = []
               
                # Settings
                D.extend(['Prompt:\n', p.prompt, '\n'])
                D.extend(['Negative prompt:\n', p.negative_prompt, '\n'])
                D.append('\n')
                D.extend(['Width: ', str(p.width), '\n'])
                D.extend(['Height: ', str(p.height), '\n'])
                D.extend(['Sampler: ', p.sampler_name, '\n'])
                D.extend(['Steps: ', str(p.steps), '\n'])
                D.extend(['CFG scale: ', str(p.cfg_scale), '\n'])
                D.extend(['Seed: ', str(p.seed), '\n'])
                D.append('---------------------------------------\n')
                # Seed Travel Settings
                D.extend(['Destination seeds: ', dest_seed, '\n'])
                D.extend(['Use random seeds: ', str(rnd_seed), '\n'])
                D.extend(['Number of random seeds: ', str(int(seed_count)), '\n'])
                D.extend(['Steps: ', str(int(steps)), '\n'])
                D.extend(['Loop back to initial seed: ', str(loopback), '\n'])
                D.extend(['FPS: ', str(video_fps), '\n'])
                D.extend(['Lead in/out: ', str(int(lead_inout)), '\n'])
                D.extend(['SSIM threshold: ', str(ssim_diff), '\n'])
                D.extend(['SSIM CenterCrop%: ', str(ssim_ccrop), '\n'])
                D.extend(['RIFE passes: ', str(int(rife_passes)), '\n'])
                D.extend(['Drop original frames: ', str(rife_drop), '\n'])
                D.extend(['Interpolation curve: ', curve, '\n'])
                D.extend(['Curve strength: ', str(curvestr), '\n'])
                D.extend(['Upscaler: ', upscale_meth, '\n'])
                D.extend(['Upscale ratio: ', str(upscale_ratio), '\n'])
                D.extend(['Compare paths: ', str(compare_paths), '\n'])
                D.extend(['Bump seed: ', str(bump_seed), '\n'])
                D.extend(['SSIM min substep: ', str(substep_min), '\n'])
                D.extend(['SSIM min threshold: ', str(ssim_diff_min), '\n'])
                D.append('---------------------------------------\n')
                # Generation stats
                D.extend(['Used seeds: ', str(seeds), '\n'])
                if ssim_diff:
                    D.append(f"Stats: Skip count: {skip_count} Worst: {skip_ssim_min} No improvment: {not_better} Min. step: {min_step}\n")
                D.append(f"Frames: {len(step_images)}\n")

                filename = f"seed_travel-info-{travel_number:05}.txt"
                try:
                    file = open(os.path.join(travel_path, filename), 'w')
                    file.writelines(D)
                    file.close()
                except Exception as e:
                    print(f"ERROR: {e}")

            # RIFE (from https://github.com/vladmandic/rife)
            if rife_passes:
                rifemodel = None
                device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
                count = 0
    
                def rifeload(model_path: str = os.path.dirname(os.path.abspath(__file__)) + '/rife/flownet-v46.pkl', fp16: bool = False):
                    global rifemodel # pylint: disable=global-statement
                    torch.set_grad_enabled(False)
                    if torch.cuda.is_available():
                        torch.backends.cudnn.enabled = True
                        torch.backends.cudnn.benchmark = True
                        if fp16:
                            torch.set_default_tensor_type(torch.cuda.HalfTensor)
                    rifemodel = Model()
                    rifemodel.load_model(model_path, -1)
                    rifemodel.eval()
                    rifemodel.device()
    
                def execute(I0, I1, n):
                    global rifemodel # pylint: disable=global-statement
                    if rifemodel.version >= 3.9:
                        res = []
                        for i in range(n):
                            res.append(rifemodel.inference(I0, I1, (i+1) * 1. / (n+1), scale))
                        return res
                    else:
                        middle = rifemodel.inference(I0, I1, scale)
                        if n == 1:
                            return [middle]
                        first_half = execute(I0, middle, n=n//2)
                        second_half = execute(middle, I1, n=n//2)
                        if n % 2:
                            return [*first_half, middle, *second_half]
                        else:
                            return [*first_half, *second_half]
    
                def pad(img):
                    return F.pad(img, padding).half() if fp16 else F.pad(img, padding)
    
                rife_images = step_images
    
                for i in range(int(rife_passes)):
                    print(f"RIFE pass {i+1}")
                    if rifemodel is None:
                        rifeload()
                    print('Interpolating', len(rife_images), 'images')
                    frame = rife_images[0]
                    w, h = tgt_w, tgt_h
                    scale = 1.0
                    fp16 = False
    
                    tmp = max(128, int(128 / scale))
                    ph = ((h - 1) // tmp + 1) * tmp
                    pw = ((w - 1) // tmp + 1) * tmp
                    padding = (0, pw - w, 0, ph - h)
    
                    buffer = []
    
                    I1 = pad(torch.from_numpy(np.transpose(frame, (2,0,1))).to(device, non_blocking=True).unsqueeze(0).float() / 255.)
                    for frame in rife_images:
                        I0 = I1
                        I1 = pad(torch.from_numpy(np.transpose(frame, (2,0,1))).to(device, non_blocking=True).unsqueeze(0).float() / 255.)
                        output = execute(I0, I1, 1)
                        for mid in output:
                            mid = (((mid[0] * 255.).byte().cpu().numpy().transpose(1, 2, 0)))
                            buffer.append(np.asarray(mid[:h, :w]))
                        if not rife_drop:
                            buffer.append(np.asarray(frame))
                    #for _i in range(buffer_frames): # fill ending frames
                    #    buffer.put(frame)
                    rife_images = buffer
    
                try:
                    frames = [np.asarray(rife_images[0])] * lead_inout + [np.asarray(t) for t in rife_images] + [np.asarray(rife_images[-1])] * lead_inout
                    fps = video_fps if video_fps > 0 else len(frames) / abs(video_fps)
                    filename = f"travel-rife-{travel_number:05}.mp4"
                    writer = imageio.get_writer(os.path.join(travel_path, filename), fps=fps, quality=8)
                    for frame in frames:
                        writer.append_data(frame)
                    writer.close()
                except:
                    print(f"ERROR: Failed generating RIFE video")
            # RIFE end

        return Processed(p, images if show_images else [], p.seed, initial_info)


    def describe(self):
        return "Travel between two (or more) seeds and create a picture at each step."
