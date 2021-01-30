class Appicon < Formula
    @@release = "1.0.0"

    desc "App Icon Generator"
    homepage "https://joeblau.com/appicon"

    stable do 
        url "https://github.com/joeblau/appicon.git",
        :tag => @@release,
        :using => :git
    end

    def install
        libexec.install Dir["*"]
        bin.install_symlink "#{libexec}/bin/appicon" => "appicon"
    end
end