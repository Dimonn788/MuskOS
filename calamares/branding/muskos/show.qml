/* Animated MuskOS logo on the welcome page (step 1). */
import QtQuick 2.15
import QtQuick.Window 2.15
Item {
    width: 510; height: 510
    Canvas {
        id: stars
        anchors.fill: parent
        property real t: 0
        onPaint: {
            var ctx = getContext("2d"); ctx.reset();
            ctx.fillStyle = "#1A1622"; ctx.fillRect(0,0,width,height);
            for (var i = 0; i < 140; ++i) {
                var x = (i * 97 % width), y = (i * 53 % height);
                var a = 0.3 + 0.7 * Math.abs(Math.sin(t/40 + i));
                ctx.fillStyle = Qt.rgba(0.93, 0.92, 0.96, a);
                ctx.fillRect(x, y, 1.6, 1.6);
            }
        }
        SequentialAnimation on t {
            loops: Animation.Infinite
            NumberAnimation { from: 0; to: 240; duration: 8000 }
        }
    }
    Text {
        anchors.centerIn: parent
        text: "MuskOS"
        font.pixelSize: 72; font.bold: true; color: "#EDEAF5"
        Behavior on opacity { NumberAnimation { duration: 1200 } }
        SequentialAnimation on scale {
            loops: Animation.Infinite
            NumberAnimation { from: 0.96; to: 1.03; duration: 2400; easing.type: Easing.InOutSine }
            NumberAnimation { from: 1.03; to: 0.96; duration: 2400; easing.type: Easing.InOutSine }
        }
    }
}
