export namespace main {
	
	export class EncodeSettings {
	    hw_mode: string;
	    quality: number;
	    preset: string;
	    look_ahead: boolean;
	    sample_interval: number;
	    suffix: string;
	    overwrite: boolean;
	
	    static createFrom(source: any = {}) {
	        return new EncodeSettings(source);
	    }
	
	    constructor(source: any = {}) {
	        if ('string' === typeof source) source = JSON.parse(source);
	        this.hw_mode = source["hw_mode"];
	        this.quality = source["quality"];
	        this.preset = source["preset"];
	        this.look_ahead = source["look_ahead"];
	        this.sample_interval = source["sample_interval"];
	        this.suffix = source["suffix"];
	        this.overwrite = source["overwrite"];
	    }
	}
	export class FFmpegStatus {
	    found: boolean;
	    path: string;
	    qsv_avail: boolean;
	    vt_avail: boolean;
	    platform: string;
	
	    static createFrom(source: any = {}) {
	        return new FFmpegStatus(source);
	    }
	
	    constructor(source: any = {}) {
	        if ('string' === typeof source) source = JSON.parse(source);
	        this.found = source["found"];
	        this.path = source["path"];
	        this.qsv_avail = source["qsv_avail"];
	        this.vt_avail = source["vt_avail"];
	        this.platform = source["platform"];
	    }
	}
	export class VideoInfo {
	    path: string;
	    name: string;
	    width: number;
	    height: number;
	    video_codec: string;
	    audio_codec: string;
	    duration: number;
	    is_10bit: boolean;
	    pix_fmt: string;
	    crop: string;
	    crop_auto: string;
	
	    static createFrom(source: any = {}) {
	        return new VideoInfo(source);
	    }
	
	    constructor(source: any = {}) {
	        if ('string' === typeof source) source = JSON.parse(source);
	        this.path = source["path"];
	        this.name = source["name"];
	        this.width = source["width"];
	        this.height = source["height"];
	        this.video_codec = source["video_codec"];
	        this.audio_codec = source["audio_codec"];
	        this.duration = source["duration"];
	        this.is_10bit = source["is_10bit"];
	        this.pix_fmt = source["pix_fmt"];
	        this.crop = source["crop"];
	        this.crop_auto = source["crop_auto"];
	    }
	}

}

