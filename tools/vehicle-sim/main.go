// Command vehicle-sim impersonates a Tesla vehicle streaming Fleet Telemetry.
//
// It dials a fleet-telemetry server over mutual TLS exactly like a car does,
// using a client certificate issued by a fake "Tesla Motors Products CA"
// (generated with fleet-telemetry's own tools/main.go) and trusting the server
// CA that the bridge app hands out in its vehicle config. It is a development
// and CI tool only: production servers never trust the fake vehicle CA.
package main

import (
	"crypto/tls"
	"crypto/x509"
	"flag"
	"fmt"
	"log"
	"math"
	"net/http"
	"net/url"
	"os"
	"time"

	"github.com/gorilla/websocket"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/teslamotors/fleet-telemetry/messages/tesla"
	"github.com/teslamotors/fleet-telemetry/protos"
)

const deviceType = "vehicle_device"

type options struct {
	host       string
	connect    string
	port       int
	vin        string
	certFile   string
	keyFile    string
	caFile     string
	scenario   string
	interval   time.Duration
	iterations int
	resend     bool
}

func main() {
	var o options
	flag.StringVar(&o.host, "host", "localhost", "fleet-telemetry hostname (must match the server certificate SAN)")
	flag.StringVar(&o.connect, "connect", "", "address to dial instead of -host (TLS still verifies -host)")
	flag.IntVar(&o.port, "port", 8443, "fleet-telemetry port")
	flag.StringVar(&o.vin, "vin", "device-1", "VIN; must equal the client certificate CN")
	flag.StringVar(&o.certFile, "cert", "", "vehicle client certificate (PEM)")
	flag.StringVar(&o.keyFile, "key", "", "vehicle client key (PEM)")
	flag.StringVar(&o.caFile, "ca", "", "server CA chain to trust (the config 'ca')")
	flag.StringVar(&o.scenario, "scenario", "types", "types | drive | charge | park")
	flag.DurationVar(&o.interval, "interval", time.Second, "delay between records")
	flag.IntVar(&o.iterations, "iterations", 5, "records to send for looping scenarios")
	flag.BoolVar(&o.resend, "resend", false, "mark records as resends")
	flag.Parse()

	conn, err := dial(o)
	if err != nil {
		log.Fatalf("dial: %v", err)
	}
	defer conn.Close()

	acks := make(chan string, 64)
	go readAcks(conn, acks)

	records := buildScenario(o)
	for i, data := range records {
		if err := send(conn, o, data); err != nil {
			log.Fatalf("send %d: %v", i, err)
		}
		select {
		case ack := <-acks:
			log.Printf("record %d: %d fields, server replied %s", i, len(data), ack)
		case <-time.After(5 * time.Second):
			log.Printf("record %d: %d fields, no reply within 5s", i, len(data))
		}
		if i < len(records)-1 {
			time.Sleep(o.interval)
		}
	}
	_ = conn.WriteMessage(websocket.CloseMessage, websocket.FormatCloseMessage(websocket.CloseNormalClosure, ""))
}

func dial(o options) (*websocket.Conn, error) {
	cert, err := tls.LoadX509KeyPair(o.certFile, o.keyFile)
	if err != nil {
		return nil, fmt.Errorf("load client cert: %w", err)
	}
	roots := x509.NewCertPool()
	caPEM, err := os.ReadFile(o.caFile)
	if err != nil {
		return nil, fmt.Errorf("read ca: %w", err)
	}
	if !roots.AppendCertsFromPEM(caPEM) {
		return nil, fmt.Errorf("no certificates in %s", o.caFile)
	}
	dialer := websocket.Dialer{
		HandshakeTimeout: 30 * time.Second,
		TLSClientConfig: &tls.Config{
			Certificates: []tls.Certificate{cert},
			RootCAs:      roots,
			ServerName:   o.host,
			MinVersion:   tls.VersionTLS12,
		},
	}
	target := o.host
	if o.connect != "" {
		target = o.connect
	}
	u := url.URL{Scheme: "wss", Host: fmt.Sprintf("%s:%d", target, o.port), Path: "/"}
	headers := http.Header{}
	headers.Add("X-Network-Interface", "wifi")
	headers.Add("Version", "1.3.0")
	conn, _, err := dialer.Dial(u.String(), headers)
	return conn, err
}

func readAcks(conn *websocket.Conn, acks chan<- string) {
	for {
		_, msg, err := conn.ReadMessage()
		if err != nil {
			close(acks)
			return
		}
		acks <- fmt.Sprintf("%d-byte ack", len(msg))
	}
}

func send(conn *websocket.Conn, o options, data []*protos.Datum) error {
	payload, err := proto.Marshal(&protos.Payload{
		Data:      data,
		CreatedAt: timestamppb.Now(),
		Vin:       o.vin,
		IsResend:  o.resend,
	})
	if err != nil {
		return err
	}
	sender := []byte(fmt.Sprintf("%s.%s", deviceType, o.vin))
	msg := tesla.FlatbuffersStreamToBytes(
		sender, []byte("V"), []byte(fmt.Sprintf("sim-%d", time.Now().UnixNano())), payload, 1,
		[]byte(fmt.Sprintf("msg-%d", time.Now().UnixNano())), []byte(deviceType), []byte(o.vin),
		uint64(time.Now().UnixMilli()),
	)
	return conn.WriteMessage(websocket.BinaryMessage, msg)
}

func datum(key protos.Field, v *protos.Value) *protos.Datum {
	return &protos.Datum{Key: key, Value: v}
}

func dbl(x float64) *protos.Value {
	return &protos.Value{Value: &protos.Value_DoubleValue{DoubleValue: x}}
}
func flt(x float32) *protos.Value {
	return &protos.Value{Value: &protos.Value_FloatValue{FloatValue: x}}
}
func i32(x int32) *protos.Value { return &protos.Value{Value: &protos.Value_IntValue{IntValue: x}} }
func i64(x int64) *protos.Value { return &protos.Value{Value: &protos.Value_LongValue{LongValue: x}} }
func str(x string) *protos.Value {
	return &protos.Value{Value: &protos.Value_StringValue{StringValue: x}}
}
func boolean(x bool) *protos.Value {
	return &protos.Value{Value: &protos.Value_BooleanValue{BooleanValue: x}}
}
func invalid() *protos.Value { return &protos.Value{Value: &protos.Value_Invalid{Invalid: true}} }
func location(lat, lon float64) *protos.Value {
	return &protos.Value{Value: &protos.Value_LocationValue{LocationValue: &protos.LocationValue{Latitude: lat, Longitude: lon}}}
}

// buildScenario returns the records (each a list of changed fields) to send.
func buildScenario(o options) [][]*protos.Datum {
	switch o.scenario {
	case "types":
		// One record exercising every value shape the bridge has to normalize.
		return [][]*protos.Datum{{
			datum(protos.Field_VehicleName, str("Sim Car")),
			datum(protos.Field_BatteryLevel, dbl(71.25)),
			datum(protos.Field_Soc, flt(70.5)),
			datum(protos.Field_ChargeLimitSoc, i32(80)),
			datum(protos.Field_Odometer, dbl(12345.6)),
			datum(protos.Field_LifetimeEnergyChargedKwh, i64(9876)),
			datum(protos.Field_Locked, boolean(true)),
			datum(protos.Field_Location, location(37.3947, -122.1503)),
			datum(protos.Field_Gear, &protos.Value{Value: &protos.Value_ShiftStateValue{ShiftStateValue: protos.ShiftState_ShiftStateP}}),
			datum(protos.Field_DetailedChargeState, &protos.Value{Value: &protos.Value_DetailedChargeStateValue{DetailedChargeStateValue: protos.DetailedChargeStateValue_DetailedChargeStateCharging}}),
			datum(protos.Field_SentryMode, &protos.Value{Value: &protos.Value_SentryModeStateValue{SentryModeStateValue: protos.SentryModeState_SentryModeStateArmed}}),
			datum(protos.Field_DoorState, &protos.Value{Value: &protos.Value_DoorValue{DoorValue: &protos.Doors{DriverFront: true, TrunkRear: true}}}),
			datum(protos.Field_FdWindow, &protos.Value{Value: &protos.Value_WindowStateValue{WindowStateValue: protos.WindowState_WindowStatePartiallyOpen}}),
			datum(protos.Field_TpmsHardWarnings, &protos.Value{Value: &protos.Value_TireLocationValue{TireLocationValue: &protos.TireLocation{FrontLeft: true}}}),
			datum(protos.Field_ScheduledChargingStartTime, &protos.Value{Value: &protos.Value_TimeValue{TimeValue: &protos.Time{Hour: 23, Minute: 30, Second: 0}}}),
			datum(protos.Field_InsideTemp, invalid()),
			datum(protos.Field_SeatHeaterLeft, i32(2)),
			datum(protos.Field_EstBatteryRange, str("1.23e+02")),
			datum(protos.Field_HvacPower, &protos.Value{Value: &protos.Value_HvacPowerValue{HvacPowerValue: protos.HvacPowerState_HvacPowerStateOn}}),
			datum(protos.Field_ClimateKeeperMode, &protos.Value{Value: &protos.Value_ClimateKeeperModeValue{ClimateKeeperModeValue: protos.ClimateKeeperModeState_ClimateKeeperModeStateDog}}),
		}}
	case "drive":
		var out [][]*protos.Datum
		for i := 0; i < o.iterations; i++ {
			t := float64(i)
			out = append(out, []*protos.Datum{
				datum(protos.Field_Gear, &protos.Value{Value: &protos.Value_ShiftStateValue{ShiftStateValue: protos.ShiftState_ShiftStateD}}),
				datum(protos.Field_VehicleSpeed, dbl(25+10*math.Sin(t))),
				datum(protos.Field_Location, location(37.3947+0.001*t, -122.1503+0.001*t)),
				datum(protos.Field_GpsHeading, dbl(math.Mod(45+t*3, 360))),
				datum(protos.Field_BatteryLevel, dbl(71.25-0.1*t)),
			})
		}
		return out
	case "charge":
		var out [][]*protos.Datum
		for i := 0; i < o.iterations; i++ {
			t := float64(i)
			out = append(out, []*protos.Datum{
				datum(protos.Field_DetailedChargeState, &protos.Value{Value: &protos.Value_DetailedChargeStateValue{DetailedChargeStateValue: protos.DetailedChargeStateValue_DetailedChargeStateCharging}}),
				datum(protos.Field_ACChargingPower, dbl(7.6)),
				datum(protos.Field_ChargeAmps, dbl(32)),
				datum(protos.Field_BatteryLevel, dbl(50+t)),
			})
		}
		return out
	case "park":
		return [][]*protos.Datum{{
			datum(protos.Field_Gear, &protos.Value{Value: &protos.Value_ShiftStateValue{ShiftStateValue: protos.ShiftState_ShiftStateP}}),
			datum(protos.Field_VehicleSpeed, invalid()),
			datum(protos.Field_Locked, boolean(true)),
		}}
	default:
		log.Fatalf("unknown scenario %q", o.scenario)
		return nil
	}
}
