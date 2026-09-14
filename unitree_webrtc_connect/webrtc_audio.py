
from aiortc import AudioStreamTrack, RTCRtpSender
import logging

# `sounddevice`, `numpy` and `wave` used to be imported here but none of them
# are referenced anywhere in this module. They only served to drag PortAudio
# (a native dependency that needs OS packages) and numpy into every install,
# including the humanoids — which have no audio channel at all. The actual
# playback helpers live in the Go2 examples and import what they need
# themselves. Keep this module import-light so R1/G1 can run without the
# `[audio]` extra.


class WebRTCAudioChannel:
    def __init__(self, pc, datachannel) -> None:
        self.pc = pc
        self.pc.addTransceiver("audio", direction="sendrecv")
        self.datachannel = datachannel

        # List to hold multiple callbacks
        self.track_callbacks = []
        
    async def frame_handler(self, frame):
        logging.info("Receiving audio frame")

        # Trigger all registered callbacks
        for callback in self.track_callbacks:
            try:
                # Call each callback function and pass the track
                await callback(frame)
            except Exception as e:
                logging.error(f"Error in callback {callback}: {e}")
    
    def add_track_callback(self, callback):
        """
        Adds a callback to be triggered when an audio track is received.
        """
        if callable(callback):
            self.track_callbacks.append(callback)
        else:
            logging.warning(f"Callback {callback} is not callable.")  

    def switchAudioChannel(self, switch: bool):
        self.datachannel.switchAudioChannel(switch)
    
        