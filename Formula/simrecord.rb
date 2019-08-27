class Simrecord < Formula
    @@release = "1.0.0"

    desc "iPhone Simulator Recorder"
    homepage "https://joeblau.com/simrecord"

    stable do 
        url "https://github.com/joeblau/simrecord.git",
        :tag => @@release,
        :using => :git
    end

    def install
        libexec.install Dir["*"]
        bin.install_symlink "#{libexec}/bin/simrecord" => "simrecord"
    end
end