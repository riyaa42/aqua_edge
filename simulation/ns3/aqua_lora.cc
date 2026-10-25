// Aqua Edge in ns-3 (LoRaWAN module): building B, 21 nodes, one gateway outside.
//
//   ./ns3 run "aqua_lora --positions=positions.csv --out=link_table.csv"
//       link table: spreading factor each node needs, signal strength, and how many of
//       200 lone test packets reach the gateway (no other traffic, so no collisions)
//   ./ns3 run "aqua_lora --positions=positions.csv --replay=tx_log.csv --out=replay_result.csv"
//       replay: every uplink of a live run, sent at the same second with the same size and SF,
//       so ns-3's own collision and capture rules can be compared with live/radio.py
//
// Packets go straight to each node's radio (PHY), skipping the LoRaWAN MAC: the firmware
// already chose the send times (time slots, listen-before-talk) and those are in tx_log.csv.

#include "ns3/buildings-module.h"
#include "ns3/core-module.h"
#include "ns3/lorawan-module.h"
#include "ns3/mobility-helper.h"

#include <fstream>
#include <map>
#include <sstream>

using namespace ns3;
using namespace lorawan;

static const uint32_t FREQ_HZ = 868100000; // ponytail: EU868 channel; module has no IN865 plan, same physics
static const double TX_DBM = 14;
static const int TEST_PACKETS = 200, TEST_BYTES = 75;

static std::map<uint64_t, std::string> g_result; // packet id -> ok / collision / weak_link

static std::vector<std::vector<std::string>>
ReadCsv(const std::string& path)
{
    std::ifstream f(path);
    NS_ABORT_MSG_IF(!f, "cannot open " << path);
    std::vector<std::vector<std::string>> rows;
    for (std::string line; std::getline(f, line);)
    {
        if (!line.empty() && line.back() == '\r') // Python's csv writes \r\n
        {
            line.pop_back();
        }
        std::vector<std::string> cells;
        std::stringstream ss(line);
        for (std::string c; std::getline(ss, c, ',');)
        {
            cells.push_back(c);
        }
        rows.push_back(cells);
    }
    return rows;
}

static void
Mark(std::string result, Ptr<const Packet> p, uint32_t)
{
    g_result[p->GetUid()] = result;
}

static void
SendAt(Ptr<LoraPhy> phy, uint32_t bytes, uint8_t sf, uint64_t* id)
{
    LoraTxParameters tx;
    tx.spreadingFactor = sf;
    tx.lowDataRateOptimize = sf >= 11;
    Ptr<Packet> p = Create<Packet>(bytes);
    *id = p->GetUid();
    phy->Send(p, FREQ_HZ, IQPolarity::UP, tx, TX_DBM);
}

int
main(int argc, char* argv[])
{
    std::string positions = "positions.csv", replay, out = "link_table.csv";
    CommandLine cmd;
    cmd.AddValue("positions", "node,x,y,z (from positions.py)", positions);
    cmd.AddValue("replay", "tx_log.csv of a live run; empty = build the link table", replay);
    cmd.AddValue("out", "output CSV", out);
    cmd.Parse(argc, argv);
    RngSeedManager::SetSeed(1); // same answer every run

    // Channel: distance loss + shadowing + walls/floors of the building (as complete-network-example)
    auto loss = CreateObject<LogDistancePropagationLossModel>();
    loss->SetPathLossExponent(3.76);
    loss->SetReference(1, 7.7);
    auto shadowing = CreateObject<CorrelatedShadowingPropagationLossModel>();
    loss->SetNext(shadowing);
    shadowing->SetNext(CreateObject<BuildingPenetrationLoss>());
    auto channel = CreateObject<LoraChannel>(loss, CreateObject<ConstantSpeedPropagationDelayModel>());

    // Building B: 18 x 18 m, 5 floors of 3 m, 2 x 2 flats per floor (webots/make_world.py)
    auto b = CreateObject<Building>();
    b->SetBoundaries(Box(-9, 9, -9, 9, 0, 15));
    b->SetBuildingType(Building::Residential);
    b->SetExtWallsType(Building::ConcreteWithWindows);
    b->SetNFloors(5);
    b->SetNRoomsX(2);
    b->SetNRoomsY(2);

    std::vector<std::string> names;
    auto where = CreateObject<ListPositionAllocator>();
    Vector gw;
    for (auto& r : ReadCsv(positions))
    {
        if (r[0] == "node")
        {
            continue;
        }
        Vector v(std::stod(r[1]), std::stod(r[2]), std::stod(r[3]));
        if (r[0] == "GATEWAY")
        {
            gw = v;
            continue;
        }
        names.push_back(r[0]);
        where->Add(v);
    }
    where->Add(gw); // gateway last

    NodeContainer eds, gws;
    eds.Create(names.size());
    gws.Create(1);
    MobilityHelper mobility;
    mobility.SetPositionAllocator(where);
    mobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
    mobility.Install(eds);
    mobility.Install(gws);
    BuildingsHelper::Install(eds);
    BuildingsHelper::Install(gws);

    LoraPhyHelper phy;
    phy.SetChannel(channel);
    LorawanMacHelper mac;
    LoraHelper lora;
    phy.SetDeviceType(LoraPhyHelper::ED);
    mac.SetDeviceType(LorawanMacHelper::ED_A);
    lora.Install(phy, mac, eds);
    phy.SetDeviceType(LoraPhyHelper::GW);
    mac.SetDeviceType(LorawanMacHelper::GW);
    lora.Install(phy, mac, gws);
    LorawanMacHelper::SetSpreadingFactorsUp(eds, gws, channel); // lowest SF that reaches the gateway

    auto gwPhy = DynamicCast<LoraNetDevice>(gws.Get(0)->GetDevice(0))->GetPhy();
    gwPhy->SetReceiveOkCallback(MakeNullCallback<void, Ptr<const Packet>>()); // raw packets: no MAC above
    gwPhy->TraceConnectWithoutContext("ReceivedPacket", MakeBoundCallback(&Mark, std::string("ok")));
    gwPhy->TraceConnectWithoutContext("LostPacketBecauseInterference", MakeBoundCallback(&Mark, std::string("collision")));
    gwPhy->TraceConnectWithoutContext("LostPacketBecauseUnderSensitivity", MakeBoundCallback(&Mark, std::string("weak_link")));
    gwPhy->TraceConnectWithoutContext("LostPacketBecauseNoMoreReceivers", MakeBoundCallback(&Mark, std::string("collision")));

    std::map<std::string, uint32_t> index;
    std::vector<std::vector<uint64_t>> ids(names.size(), std::vector<uint64_t>(TEST_PACKETS));
    std::vector<std::vector<std::string>> rows; // replay: the uplink rows of tx_log.csv
    for (uint32_t i = 0; i < names.size(); i++)
    {
        index[names[i]] = i;
    }
    auto edPhy = [&](uint32_t i) { return DynamicCast<LoraNetDevice>(eds.Get(i)->GetDevice(0))->GetPhy(); };
    auto edSf = [&](uint32_t i) {
        auto m = DynamicCast<EndDeviceLorawanMac>(DynamicCast<LoraNetDevice>(eds.Get(i)->GetDevice(0))->GetMac());
        return uint8_t(12 - m->GetDataRate()); // EU868: DR0 = SF12 ... DR5 = SF7
    };

    if (replay.empty())
    {
        for (uint32_t i = 0; i < names.size(); i++) // one node at a time, 5 s apart: never overlap
        {
            for (int k = 0; k < TEST_PACKETS; k++)
            {
                Simulator::Schedule(Seconds(5.0 * (i * TEST_PACKETS + k) + 1), &SendAt, edPhy(i), TEST_BYTES,
                                    edSf(i), &ids[i][k]);
            }
        }
    }
    else
    {
        auto log = ReadCsv(replay); // t_s,src,dst,kind,seq,bytes,sf,ch,airtime_ms,backoffs,result
        for (auto& r : log)
        {
            if (r[0] == "t_s" || r[2] != "GATEWAY" || r.back() == "radio_cut" || !index.count(r[1]))
            {
                continue;
            }
            rows.push_back(r);
        }
        std::vector<uint64_t> order(rows.size());
        for (size_t k = 0; k < rows.size(); k++)
        {
            Simulator::Schedule(Seconds(std::stod(rows[k][0])), &SendAt, edPhy(index[rows[k][1]]),
                                std::stoul(rows[k][5]), uint8_t(std::stoi(rows[k][6])), &order[k]);
        }
        Simulator::Run();
        std::ofstream f(out);
        f << "src,t_s,result\n";
        for (size_t k = 0; k < rows.size(); k++)
        {
            auto it = g_result.find(order[k]);
            f << rows[k][1] << "," << rows[k][0] << "," << (it == g_result.end() ? "lost" : it->second) << "\n";
        }
        Simulator::Destroy();
        return 0;
    }
    Simulator::Run();

    std::ofstream f(out);
    auto gwMob = gws.Get(0)->GetObject<MobilityModel>();
    f << "node,sf,pdr,rssi_dbm\n";
    for (uint32_t i = 0; i < names.size(); i++)
    {
        int ok = 0;
        for (auto id : ids[i])
        {
            ok += g_result[id] == "ok";
        }
        double rssi = channel->GetRxPower(TX_DBM, eds.Get(i)->GetObject<MobilityModel>(), gwMob);
        f << names[i] << "," << int(edSf(i)) << "," << double(ok) / TEST_PACKETS << "," << rssi << "\n";
    }
    Simulator::Destroy();
    return 0;
}
