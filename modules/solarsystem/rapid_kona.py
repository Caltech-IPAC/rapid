#Known Object Name Association (KONA) for labeling Solar system objects in
#the RAPID processing pipeline 

#2024-10-01 - Joseph Masiero, based on prototype by Manaswi Kondapally
#2025-01-15 - Revised to hook better into RAPID pipeline


import kete
import click
import sys
import logging
import os
import asdf

#put at the very top, and point to where ever we want the orbit files cached
#there is also an option to make this an environment variable instead
kete.cache.Cached_Directory("./")


def kona(input_files,mpc_local=None,median_jd=None,mpc_save=None,logger=None):
    #input_files should be a list, or a comma-seperated string of full-paths
    
    if logger==None:
        logger=init_log()

    if mpc_local is not None and os.path.exists(mpc_local):
        #in the case that a file with the MPC orbits moved to a central date is provided, read that
        logger.info(f"Reading state file: {mpc_local:s}")
        mpc_states=kete.SimultaneousStates.load_parquet(mpc_local).states
    else:
        #if not, pull a new file from MPC
        logger.info("Fetching orbits from MPC")
        orbits = kete.horizons.fetch_known_orbit_data()
        #comet_orbits = kete.mpc.fetch_known_comet_orbit_data()
        mpc_states = kete.mpc.table_to_states(orbits)# + kete.mpc.table_to_states(comet_orbits) 

    if median_jd is not None:
        #move the orbits to a local date. If you don't do this, individual runs will potentially take a long time
        logger.info(f"Moving all states to JD={median_jd:.5f}")
        mpc_states_local  = kete.propagate_n_body(mpc_states, median_jd)
    else:
        mpc_states_local = mpc_states

    if mpc_save is not None:
        #if an MPC save parameter is included, save a new local state file        
        logger.info(f"Saving MPC state file: {mpc_save:s}")
        if os.path.exists(mpc_save):
            logger.warn(f"{mpc_save:s} path exists: overwriting")
        kete.SimultaneousStates(mpc_states_local).save_parquet(mpc_save)

    if type(input_files) is str:
        input_files=input_files.split(',')

    fovs=[]
    trees=[]
    for input_file in input_files:
        in_tree=asdf.open(input_file)
        trees.append(in_tree)
        
        image_time=kete.Time.from_mjd(in_tree["roman"]["meta"]["exposure"]["mid_time_mjd"])
    
        scx=in_tree["roman"]["meta"]["ephemeris"]["spatial_x"]/kete.constants.AU_KM
        scy=in_tree["roman"]["meta"]["ephemeris"]["spatial_y"]/kete.constants.AU_KM
        scz=in_tree["roman"]["meta"]["ephemeris"]["spatial_z"]/kete.constants.AU_KM
        scvx=in_tree["roman"]["meta"]["ephemeris"]["velocity_x"]/kete.constants.AU_KM
        scvy=in_tree["roman"]["meta"]["ephemeris"]["velocity_y"]/kete.constants.AU_KM
        scvz=in_tree["roman"]["meta"]["ephemeris"]["velocity_z"]/kete.constants.AU_KM
    
        headerframe=in_tree["roman"]["meta"]["ephemeris"]["ephemeris_reference_frame"]
        if headerframe=="Ecliptic":
            frame=kete.Frames.Ecliptic
        else:
            frame=kete.Frames.Equatorial
            
        pos = kete.Vector([scx, scy, scz], frame=frame)
        vel = kete.Vector([scvx, scvy, scvz], frame=frame)
        sc_state = kete.State("Roman-earth", image_time, pos, vel)
        earth = kete.spice.get_state("Earth", image_time.jd).as_equatorial
        
        obs_pos=earth.pos+sc_state.pos        
        obs_vel=earth.vel+sc_state.vel
        obs_loc=kete.State("Roman-helio",image_time,obs_pos,obs_vel)
    
        ra0=in_tree["roman"]["meta"]["pointng"]["ra_v1"] #deg
        dec0=in_tree["roman"]["meta"]["pointng"]["dec_v1"] #deg
        pointing_vec=kete.Vector.from_ra_dec(ra0, dec0)
        
        fov = kete.fov.ConeFOV(pointing_vec,0.5,obs_loc)
        fovs.append(fov)
        
    curr_states  = kete.propagate_n_body(mpc_states_local, image_time.jd)

    #this should be a bulk query grouping all FoV into the below list for sig speed up
    visible_objs = kete.fov_state_check(curr_states,fovs)


    for visible_obj, in_tree, input_file in zip(visible_objs,trees,input_files):
        logger.info(f"Found {len(visible_obj.states):d} visible objects near the FoV")
    
        obj_in_fov={}
        if len(visible_obj.states)>0:
            logger.info(f"{'Name':<15} {'RA':<10} {'DEC':<10}")
            logger.info("-"*45)
            for state in visible_obj.states:
                vec = (state.pos - visible_obj.fov.observer.pos).as_equatorial
                logger.info(f"{state.desig:15s} {vec.ra:10.6f} {vec.dec:+9.6f}")
                obj_in_fov[state.desig]=(vec.ra,vec.dec)

        #^*^
        #Now add the list of found objects with RA/Dec to the ASDF metadata
        in_tree["roman"]["meta"]["rapid"]["sso_kona"]=obj_in_fov
        in_tree.write_to(input_file)
        


def init_log(logfile=None):
    if logfile is None:
        logger=logging.getLogger()
        ch = logging.StreamHandler()
    else:
        name=logfile
        logger=logging.getLogger(name)
        ch = logging.FileHandler(filename=logfile)
    logger.setLevel(logging.INFO)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logger.addHandler(ch)
    return(logger)

        
class helpExit(click.Command):
    def get_help(self, ctx):
        helpstr=super().get_help(ctx)
        click.echo(helpstr)
        sys.exit(1)
 
@click.command(no_args_is_help=True,cls=helpExit)
@click.option("--input_file",help="<input image ASDF file to run KONA on>",required=True)
@click.option("--mpc_local",help="[full path to parquet file holding the MPC orbit file propagated to a median JD]",required=False,default=None)
@click.option("--median_jd",help="[median Julian Date to advance the MPC orbit file to]",required=False,default=None,type=float)
@click.option("--mpc_save",help="[full path to parquet output file to store the MPC orbit states after moving to median_jd]",required=False,default=None)
def bespoke_kona(input_file,mpc_local,median_jd,mpc_save):
    logger=init_log()
    logger.info("command echo:")
    logger.info(sys.argv)
    call_back=kona(input_file=input_file,mpc_local=mpc_local,median_jd=median_jd,mpc_save=mpc_save,logger=logger)
    if call_back>0:
        print("***Error encountered in rapid_kona: {:d}".format(call_back))
        sys.exit(call_back)


if __name__ == "__main__":
    # execute only if run as a script
    bespoke_kona()
        
